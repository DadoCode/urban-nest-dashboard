"""Apply a month's import plan, record exactly what changed, and undo it.

An import never deletes anything it does not replace: it touches only the
workbook/Excel-sourced rows of the property-months in the plan (uploaded and
hand-entered rows are left alone). Every row it adds, and a full copy of every
row it removes, is logged against the batch, so Undo can put the ledger back
exactly as it was.
"""
import datetime
import json

from services import audit, sources as src, vendors
from services.completeness import seed_defaults
from . import config as C
from . import identity
from . import plan as P

_TX_COLS = ("property_id", "date", "vendor", "description", "amount", "direction", "category", "capex", "source",
            "document_id", "recurring_cost_id", "vendor_id", "edited_at", "edited_by", "import_batch_id", "source_ref")
_BK_COLS = ("property_id", "platform", "reservation_id", "check_in", "check_out", "gross_revenue", "platform_fees",
            "cleaning_fee", "net_revenue", "status", "source", "document_id", "import_batch_id", "source_ref")
_COMPARE = ("property_id", "date", "vendor", "description", "amount", "direction", "category", "capex")


class ImportRefused(Exception):
    """The import (or undo) cannot be done safely; the message says why."""


def _log(conn, batch_id, op, tbl, row_id, row):
    if batch_id is None:
        return
    conn.execute("INSERT INTO import_batch_rows (batch_id, op, tbl, row_id, row_json) VALUES (?,?,?,?,?)",
                 (batch_id, op, tbl, row_id, json.dumps(row, default=str)))


def _insert(conn, table, cols, row):
    placeholders = ",".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", [row.get(c) for c in cols])
    return cur.lastrowid


def _delete_row(conn, batch_id, table, row):
    _log(conn, batch_id, "removed", table, row["id"], row)
    conn.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))


def create_property(conn, prop, batch_id):
    """Create a property from a CONFIRMED proposal: stable id, name, model, sheet mapping and aliases. Logged for undo."""
    pid = prop["property_id"]
    if conn.execute("SELECT 1 FROM properties WHERE id=?", (pid,)).fetchone():
        return False
    code = prop["code"]
    if conn.execute("SELECT 1 FROM properties WHERE code=?", (code,)).fetchone():
        code = pid.upper()[:10]
    pct = prop["pct"] if prop["model"] == "managed" else None
    conn.execute("INSERT INTO properties (id, code, name, address, type, management_fee_pct) VALUES (?,?,?,?,'flat',?)",
                 (pid, code, prop["name"], prop["name"], pct))
    seed_defaults(conn, pid)
    _log(conn, batch_id, "property_created", "properties", None, {"id": pid, "code": code, "name": prop["name"], "model": prop["model"], "pct": pct})
    conn.execute("INSERT OR IGNORE INTO workbook_sheet_map (sheet_code, property_id, source) VALUES (?,?,?)",
                 (prop["code"], pid, f"import #{batch_id}" if batch_id else "import (preview)"))
    _log(conn, batch_id, "sheet_map_added", "workbook_sheet_map", None, {"sheet_code": prop["code"]})
    for alias in [prop["code"], prop["name"], *prop.get("aliases", [])]:
        if identity.add_alias(conn, pid, alias, "code" if alias == prop["code"] else "name"):
            _log(conn, batch_id, "alias_added", "property_identity_aliases", None, {"alias_norm": identity.norm(alias)})
    return True


def _map_sheet_if_needed(conn, item, batch_id):
    """A sheet that matched an existing property by alias becomes a permanent mapping once an import is applied."""
    code, pid = item.get("code"), item["property_id"]
    if code and conn.execute("SELECT 1 FROM workbook_sheet_map WHERE sheet_code=?", (code,)).fetchone() is None:
        conn.execute("INSERT INTO workbook_sheet_map (sheet_code, property_id, source) VALUES (?,?,?)",
                     (code, pid, f"import #{batch_id}" if batch_id else "import (preview)"))
        _log(conn, batch_id, "sheet_map_added", "workbook_sheet_map", None, {"sheet_code": code})


def write_changes(conn, item, batch_id, ym):
    """Make the ledger match the workbook for one plan item. Returns how many rows were added/removed."""
    added = removed = 0
    pid = item["property_id"]
    if item.get("new_property"):
        create_property(conn, item["new_property"], batch_id)
    elif item.get("code") and pid != P.BUSINESS_ID:
        _map_sheet_if_needed(conn, item, batch_id)
    for d in item["rows"]:
        if d["status"] in ("REMOVED", "CHANGED"):
            _delete_row(conn, batch_id, "transactions", d["old"])
            removed += 1
        if d["status"] in ("NEW", "CHANGED"):
            row = dict(d["new"])
            row["import_batch_id"] = batch_id
            row["vendor_id"] = vendors.get_or_create_vendor(conn, row.get("vendor"))
            row["id"] = _insert(conn, "transactions", _TX_COLS, row)
            _log(conn, batch_id, "added", "transactions", row["id"], row)
            added += 1
    if pid == P.BUSINESS_ID:
        return added, removed
    # monthly aggregate: the workbook's Days Booked, stored the way the history stores it
    _tx, agg = P.current_rows(conn, pid, ym)
    days = int(item.get("days") or 0)
    if P.agg_nights(agg) != days:
        for old in agg:
            _delete_row(conn, batch_id, "bookings", old)
            removed += 1
        if days > 0:
            start = datetime.date.fromisoformat(f"{ym}-01")
            row = {"property_id": pid, "platform": None, "reservation_id": "monthly-aggregate", "check_in": start.isoformat(),
                   "check_out": (start + datetime.timedelta(days=days)).isoformat(), "gross_revenue": 0.0, "platform_fees": 0.0,
                   "cleaning_fee": 0.0, "net_revenue": 0.0, "status": "confirmed", "source": "workbook",
                   "document_id": None, "import_batch_id": batch_id, "source_ref": f"Days Booked {ym}"}
            row["id"] = _insert(conn, "bookings", _BK_COLS, row)
            _log(conn, batch_id, "added", "bookings", row["id"], row)
            added += 1
    # the workbook is the active booking source for this month unless a person already chose otherwise
    if not conn.execute("SELECT 1 FROM booking_source_state WHERE property_id=? AND month=?", (pid, ym)).fetchone():
        note = f"workbook import #{batch_id}" if batch_id else "workbook import (preview)"
        conn.execute("INSERT INTO booking_source_state (property_id, month, active_source, decided_by, note) VALUES (?,?,?,?,?)",
                     (pid, ym, src.LEGACY, note, "Workbook monthly figures are authoritative for this month."))
        _log(conn, batch_id, "source_state_added", "booking_source_state", None, {"property_id": pid, "month": ym})
    return added, removed


def _save_exclusions(conn, batch_id, plan):
    ym, wanted = plan["ym"], set(plan.get("excluded") or [])
    old = [dict(r) for r in conn.execute("SELECT * FROM import_exclusions WHERE period=?", (ym,))]
    if {o["source_ref"] for o in old} == wanted:
        return
    for o in old:
        _log(conn, batch_id, "exclusion_removed", "import_exclusions", None, o)
    conn.execute("DELETE FROM import_exclusions WHERE period=?", (ym,))
    suspects = {s["ref"]: s for s in (plan["business"] or {}).get("suspects", [])}
    for ref in sorted(wanted):
        s = suspects.get(ref, {})
        conn.execute("INSERT INTO import_exclusions (period, source_ref, label, amount, batch_id) VALUES (?,?,?,?,?)",
                     (ym, ref, s.get("label"), s.get("amount"), batch_id))
        _log(conn, batch_id, "exclusion_added", "import_exclusions", None, {"period": ym, "source_ref": ref})


def _totals(view):
    return {k: view[k] for k in ("revenue", "total_expenses", "property_costs", "management_fee", "days", "occupancy", "profit") if k in view} \
        if view.get("model") != "business" else {"business_costs": view["business_costs"]}


def apply_batch(conn, batch_id, plan, selected, user="owner"):
    """Apply the selected property ids (and/or the business cost centre) from `plan`. One transaction."""
    ym = plan["ym"]
    items = [i for i in plan["properties"] + [plan["business"]] if i and i["property_id"] in selected]
    refused = [i["name"] for i in items if i["status"] not in ("ok", "review", "unchanged", "no_activity", "new_property")]
    if refused:
        raise ImportRefused("These cannot be imported: " + ", ".join(refused))
    unconfirmed = [i["name"] for i in items if i.get("new_property") and not i["new_property"]["confirmed"]]
    if unconfirmed:
        raise ImportRefused("Confirm the name and the managed / operated model (with the fee % if managed) before creating: " + ", ".join(unconfirmed))
    before, after, recon, touched, rows, created = {}, {}, {}, [], 0, []
    added = changed = superseded = 0
    try:
        for item in items:
            if item["status"] in ("unchanged", "no_activity") or item.get("change_count", 0) == 0:
                continue
            pid = item["property_id"]
            new = bool(item.get("new_property"))
            before[pid] = None if new else _totals(P.dashboard_view(conn, pid, ym))
            a, r = write_changes(conn, item, batch_id, ym)
            rows += a + r
            c = item.get("counts") or {}
            added += c.get("NEW", 0)
            changed += c.get("CHANGED", 0) + (1 if item.get("bookings", {}).get("changed") else 0)
            superseded += c.get("REMOVED", 0)
            after[pid] = _totals(P.dashboard_view(conn, pid, ym))
            recon[pid] = [{k: c_[k] for k in ("metric", "workbook", "imported", "diff", "status")} for c_ in item["checks"]]
            touched.append(pid)
            if new:
                created.append(pid)
        if not touched:
            raise ImportRefused("Nothing to import: the dashboard already matches the workbook for the selected properties.")
        if plan.get("business") and P.BUSINESS_ID in selected:
            _save_exclusions(conn, batch_id, plan)
        conn.execute(
            """UPDATE import_batches SET status='applied', applied_at=datetime('now'), period=?, properties=?, row_count=?,
               before_totals=?, after_totals=?, reconciliation=?, rows_added=?, rows_changed=?, rows_removed=?, new_properties=? WHERE id=?""",
            (ym, json.dumps(touched), rows, json.dumps(before), json.dumps(after), json.dumps(recon), added, changed, superseded,
             json.dumps(created), batch_id))
        audit.record(conn, "import_batch", batch_id, "apply", "period", None, ym, user)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"touched": touched, "rows": rows, "before": before, "after": after, "created": created}


# ------------------------------------------------------------------- undo
def _later_overlap(conn, batch):
    mine = set(json.loads(batch["properties"] or "[]"))
    for r in conn.execute("SELECT id, properties FROM import_batches WHERE status='applied' AND period=? AND id>?", (batch["period"], batch["id"])):
        if mine & set(json.loads(r["properties"] or "[]")):
            return r["id"]
    return None


def undo_batch(conn, batch_id, user="owner"):
    batch = conn.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch or batch["status"] != "applied":
        raise ImportRefused("Only an applied import can be undone.")
    later = _later_overlap(conn, batch)
    if later:
        raise ImportRefused(f"Import #{later} changed the same property-month afterwards. Undo that one first.")
    log = [dict(r) for r in conn.execute("SELECT * FROM import_batch_rows WHERE batch_id=? ORDER BY id", (batch_id,))]
    # refuse if rows this import wrote were edited or deleted since: restoring would then guess
    problems = []
    for entry in log:
        if entry["op"] != "added":
            continue
        stored = json.loads(entry["row_json"])
        cur = conn.execute(f"SELECT * FROM {entry['tbl']} WHERE id=?", (entry["row_id"],)).fetchone()
        if cur is None:
            problems.append(f"{entry['tbl']} #{entry['row_id']} no longer exists")
            continue
        cols = _COMPARE if entry["tbl"] == "transactions" else ("property_id", "check_in", "check_out", "status")
        if any(str(cur[c]) != str(stored.get(c)) and not (cur[c] is None and stored.get(c) is None) and
               not (isinstance(cur[c], float) and abs(cur[c] - float(stored.get(c) or 0)) < 1e-6) for c in cols):
            problems.append(f"{entry['tbl']} #{entry['row_id']} was edited after the import")
    # a property this import created can only go if nothing else has been recorded against it since
    created = [json.loads(e["row_json"])["id"] for e in log if e["op"] == "property_created"]
    mine = {t: {e["row_id"] for e in log if e["op"] == "added" and e["tbl"] == t} for t in ("transactions", "bookings")}
    for pid in created:
        for table, key in (("transactions", "property_id"), ("bookings", "property_id")):
            ids = {r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE {key}=?", (pid,))}
            if ids - mine[table]:
                problems.append(f"{pid} has {len(ids - mine[table])} later {table} row(s)")
        for table in ("documents", "property_targets", "property_fixed_costs"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE property_id=?", (pid,)).fetchone()[0]
            if n:
                problems.append(f"{pid} has {n} {table.replace('_', ' ')} record(s) added since")
    if problems:
        raise ImportRefused("This import cannot be undone safely: " + "; ".join(problems[:5]) + ". Revert those edits first.")
    try:
        for entry in log:
            if entry["op"] == "added":
                conn.execute(f"DELETE FROM {entry['tbl']} WHERE id=?", (entry["row_id"],))
            elif entry["op"] == "exclusion_added":
                row = json.loads(entry["row_json"])
                conn.execute("DELETE FROM import_exclusions WHERE period=? AND source_ref=?", (row["period"], row["source_ref"]))
            elif entry["op"] == "source_state_added":
                row = json.loads(entry["row_json"])
                conn.execute("DELETE FROM booking_source_state WHERE property_id=? AND month=? AND decided_by LIKE 'workbook import%'",
                             (row["property_id"], row["month"]))
        for entry in log:
            if entry["op"] == "removed":
                row = json.loads(entry["row_json"])
                cols = ("id",) + (_TX_COLS if entry["tbl"] == "transactions" else _BK_COLS)
                _insert(conn, entry["tbl"], cols, row)
            elif entry["op"] == "property_updated":
                row = json.loads(entry["row_json"])
                sets = ", ".join(f"{k}=?" for k in row["old"])
                conn.execute(f"UPDATE properties SET {sets} WHERE id=?", [*row["old"].values(), row["id"]])
            elif entry["op"] == "exclusion_removed":
                row = json.loads(entry["row_json"])
                conn.execute("INSERT OR REPLACE INTO import_exclusions (period, source_ref, label, amount, batch_id) VALUES (?,?,?,?,?)",
                             (row["period"], row["source_ref"], row.get("label"), row.get("amount"), row.get("batch_id")))
            elif entry["op"] == "alias_added":
                conn.execute("DELETE FROM property_identity_aliases WHERE alias_norm=?", (json.loads(entry["row_json"])["alias_norm"],))
            elif entry["op"] == "sheet_map_added":
                conn.execute("DELETE FROM workbook_sheet_map WHERE sheet_code=?", (json.loads(entry["row_json"])["sheet_code"],))
        for pid in created:
            for table in ("property_data_requirements", "booking_source_state", "property_identity_aliases", "workbook_sheet_map"):
                conn.execute(f"DELETE FROM {table} WHERE property_id=?", (pid,))
            conn.execute("DELETE FROM properties WHERE id=?", (pid,))
        conn.execute("UPDATE import_batches SET status='undone', undone_at=datetime('now') WHERE id=?", (batch_id,))
        audit.record(conn, "import_batch", batch_id, "undo", "status", "applied", "undone", user)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # prove the restore: today's figures against the 'before' snapshot taken at apply time
    before = {k: v for k, v in json.loads(batch["before_totals"] or "{}").items() if v is not None}
    now = {pid: _totals(P.dashboard_view(conn, pid, batch["period"])) for pid in before} if batch["period"] else {}
    return {"restored_exactly": now == before, "before": before, "now": now, "removed_properties": created}


# ------------------------------------------------------------- verification
def verify_item(conn, parsed, code, ym):
    """WORKBOOK = IMPORTED LEDGER = DASHBOARD KPI, for one property-month, after an import."""
    pid = P.pid_for(conn, parsed, code)
    prop = parsed["properties"][code]
    summary = prop["summary"].get(ym, {})
    s, e = P.bounds(ym)
    view = P.dashboard_view(conn, pid, ym)
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0.0
    ledger_income = q(f"SELECT SUM(amount) FROM transactions WHERE property_id=? AND direction='income' AND date>=? AND date<? AND source IN {P._AGG_IN}", pid, s, e)
    ledger_costs = q(f"SELECT SUM(amount) FROM transactions WHERE property_id=? AND direction='expense' AND category!='management_fee' AND date>=? AND date<? AND source IN {P._AGG_IN}", pid, s, e)
    ledger_fee = q(f"SELECT SUM(amount) FROM transactions WHERE property_id=? AND direction='expense' AND category='management_fee' AND date>=? AND date<? AND source IN {P._AGG_IN}", pid, s, e)
    # costs that came from uploaded documents or hand entry are added to the workbook's, never replaced by them
    other_costs = q(f"SELECT SUM(amount) FROM transactions WHERE property_id=? AND direction='expense' AND category!='management_fee' AND date>=? AND date<? AND source NOT IN {P._AGG_IN}", pid, s, e)
    _t, agg = P.current_rows(conn, pid, ym)
    nights = P.agg_nights(agg)
    sheet_fee = sum(i["amount"] for blk in ("opex", "capex") for i in prop[blk].get(ym, {}).get("items", []) if C.categorise(i["label"]) == "management_fee")
    main_fee = parsed["main"]["months"].get(ym, {}).get("fees", {}).get(code)
    wb_fee = main_fee if main_fee is not None else sheet_fee
    managed = view["model"] == "managed"
    if managed and not ledger_fee:                      # no fee row recorded: the dashboard estimates it from the property's percentage
        pct = conn.execute("SELECT management_fee_pct FROM properties WHERE id=?", (pid,)).fetchone()[0] or 0
        ledger_fee = round(ledger_income * pct / 100, 4)
    total_costs = None if summary.get("total_costs") is None else -summary["total_costs"]
    rows = [
        ("Income / booking revenue", summary.get("income"), ledger_income, view["revenue"]),
        ("Property costs (excl. management fee)", None if total_costs is None else total_costs - sheet_fee, ledger_costs, round(view["property_costs"] - other_costs, 2)),
        ("Management fee earned" if managed else "Management fee (n/a)", wb_fee if managed else None, ledger_fee if managed else None,
         view["management_fee"] if managed else None),
        ("Days booked", summary.get("days"), nights, view["days"]),
        ("Occupancy", summary.get("occupancy"), round(nights / P.days_in(ym), 4), view["occupancy"]),
        ("Management Fee Earned (= Property Profit)" if managed else "Property profit", wb_fee if managed else summary.get("net"),
         None, view["profit"]),
    ]
    out = []
    notes = {"Property costs (excl. management fee)": f"dashboard also holds {other_costs:.2f} from uploaded / hand-entered rows" if other_costs else ""}
    for name, wb, led, dash in rows:
        vals = [v for v in (wb, led, dash) if v is not None]
        tol = C.OCCUPANCY_TOLERANCE if name == "Occupancy" else C.TOLERANCE
        agree = len(vals) >= 2 and max(vals) - min(vals) <= tol
        if name.endswith("(n/a)"):
            continue
        # a blank workbook control total cannot confirm anything: say so instead of passing on ledger = dashboard alone
        status = "NO CONTROL" if wb is None else ("PASS" if agree else "REVIEW")
        out.append({"metric": name, "workbook": wb, "ledger": led, "dashboard": dash, "status": status, "note": notes.get(name, "")})
    return {"code": code, "property_id": pid, "model": view["model"], "rows": out}
