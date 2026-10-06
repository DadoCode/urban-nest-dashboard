"""One-off, reviewable data corrections: full display names, a wrong property model, and confirmed double-counted
business rows. Everything is written through a batch (kind='cleanup') so it can be undone exactly, and nothing is
deleted unless it is a CONFIRMED duplicate. Dry-run first (`plan_cleanup`), then `apply_cleanup`."""
import json

from . import config as C
from . import identity
from . import plan as P
from .apply import _log


def _model(row):
    return "managed" if (row["management_fee_pct"] or row["is_managed"]) else "operated"


def mapping_table(conn, year=2026):
    """CURRENT NAME / CANONICAL FULL NAME / SHORT CODE / WORKBOOK SHEET / MODEL / ALIASES / CONFIDENCE / SOURCE OF NAME."""
    rows = []
    code_of = {pid: code for code, (pid, _n) in C.PROPERTY_SHEETS.items()}
    code_of.update({cfg["pid"]: code for code, cfg in C.MAIN_ONLY_PROPERTIES.items()})
    for pid, (name, conf, source, question) in C.CANONICAL_NAMES.items():
        row = conn.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
        code = code_of.get(pid, "")
        model = _model(row) if row else "not in dashboard"
        decision = C.MODEL_DECISIONS.get(pid)
        if row and decision and (decision[0] != model or (decision[2] and row["management_fee_pct"] != decision[2])):
            model = f"{model} -> {decision[0]}" + (f" {decision[2]:g}%" if decision[2] else "")
        status_decision = C.STATUS_DECISIONS.get(pid)
        if row and status_decision and bool(row["active"]) != bool(status_decision[0]):
            model += " | active -> INACTIVE"
        aliases = sorted({code, name, *(row["name"] for _ in [0] if row), *C.PROPERTY_ALIASES.get(code, [])} - {""})
        start = C.START_DATES.get(pid)
        decided = C.NEW_PROPERTY_DEFAULTS.get(pid)
        main_only = next((cfg for cfg in C.MAIN_ONLY_PROPERTIES.values() if cfg["pid"] == pid), None)
        if not row and decided:
            model = f"{decided['model']} {decided['pct']:g}% (to be created)"
        elif not row and main_only:
            model = f"{main_only['model']}, fee % not known (to be created from the Main Page)"
        rows.append({"property_id": pid, "current_name": row["name"] if row else "(not in the dashboard)", "canonical": name, "code": code,
                     "sheet": ("Main Page only" if main_only else (f"{code}{year % 100:02d}" if code else "")), "model": model, "aliases": aliases, "confidence": conf,
                     "source": source, "question": question, "active": row["active"] if row else None, "start_date": (row["start_date"] if row else None) or (start[0] if start else None)})
    return rows


def echo_duplicates(conn):
    """Business rows that are the echo of a property's own costs: description resolves to a flat AND the amount equals
    that property's total expenses for the month (to the penny). Confirmed double counts."""
    out = []
    for r in conn.execute("SELECT id, date, description, category, amount, source FROM transactions WHERE property_id=? AND direction='expense' ORDER BY date", (P.BUSINESS_ID,)):
        pid, _how = identity.resolve(conn, r["description"])
        if not pid or pid == P.BUSINESS_ID:
            continue
        ym = r["date"][:7]
        total = conn.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE property_id=? AND direction='expense' AND substr(date,1,7)=?", (pid, ym)).fetchone()[0]
        out.append({"row": dict(r), "property_id": pid, "month": ym, "property_total": round(total, 2), "match": abs(total - r["amount"]) <= 0.011,
                    "diff": round(r["amount"] - total, 2)})
    return out


def pre_start_copies(conn):
    """Income dated before a property's start date that is an EXACT copy (same date, description and amount) of another property's
    row: an earlier sync attached it to the wrong property. Only listed properties (you confirmed NW4) are considered, and only
    rows with a twin are returned as removable; rows without a twin are reported and left."""
    removable, left = [], []
    for pid in sorted(C.PRE_START_DUPLICATE_CLEANUP):
        start = (C.START_DATES.get(pid) or (None,))[0] or (conn.execute("SELECT start_date FROM properties WHERE id=?", (pid,)).fetchone() or [None])[0]
        if not start:
            continue
        for r in conn.execute(f"""SELECT * FROM transactions WHERE property_id=? AND direction='income' AND date<? AND source IN {P._AGG_IN} ORDER BY date, id""", (pid, start)):
            twin = conn.execute("""SELECT id, property_id FROM transactions WHERE property_id!=? AND direction='income' AND date=? AND COALESCE(description,'')=COALESCE(?,'')
                                   AND ABS(amount-?)<0.005 ORDER BY id LIMIT 1""", (pid, r["date"], r["description"], r["amount"])).fetchone()
            (removable if twin else left).append({"row": dict(r), "twin": dict(twin) if twin else None})
    return removable, left


def suspect_rows(conn, min_amount=50.0):
    """Business rows that equal a property row of the same month to the penny but are NOT an echo of the whole month: for review only."""
    out = []
    for r in conn.execute("SELECT id, date, description, category, amount FROM transactions WHERE property_id=? AND direction='expense' AND ABS(amount)>=?", (P.BUSINESS_ID, min_amount)):
        for p in conn.execute("""SELECT property_id, description, amount FROM transactions WHERE property_id!=? AND direction='expense'
                                 AND substr(date,1,7)=? AND ABS(amount-?)<0.011""", (P.BUSINESS_ID, r["date"][:7], r["amount"])):
            out.append({"business": dict(r), "property": dict(p)})
    return out


def model_impact(conn, pid, new_pct):
    """Per month: the dashboard's figures for the property now, and with the model changed -- computed on a rolled-back copy."""
    months = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,7) FROM transactions WHERE property_id=? UNION SELECT DISTINCT substr(check_in,1,7) FROM bookings WHERE property_id=? ORDER BY 1", (pid, pid))]
    before = {m: P.dashboard_view(conn, pid, m) for m in months}
    with P.simulate(conn):
        conn.execute("UPDATE properties SET management_fee_pct=?, is_managed=? WHERE id=?", (new_pct, 1 if new_pct else 0, pid))
        after = {m: P.dashboard_view(conn, pid, m) for m in months}
    return [{"month": m, "before": before[m], "after": after[m]} for m in months]


def plan_cleanup(conn):
    actions = {"renames": [], "models": [], "duplicates": [], "left_alone": [], "flags": [], "start_dates": [], "distinct": [], "pre_start": [], "pre_start_left": [], "status": []}
    for pid, (name, conf, source, question) in C.CANONICAL_NAMES.items():
        row = conn.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
        if not row:
            continue
        if row["name"] != name or row["address"] != name:
            actions["renames"].append({"property_id": pid, "from": row["name"], "to": name, "from_address": row["address"]})
        if question:
            actions["flags"].append({"property_id": pid, "question": question})
    for pid, (model, why, pct) in C.MODEL_DECISIONS.items():
        row = conn.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
        if row and (_model(row) != model or (model == "managed" and pct and row["management_fee_pct"] != pct)):
            actions["models"].append({"property_id": pid, "from": _model(row), "from_pct": row["management_fee_pct"], "from_is_managed": row["is_managed"], "to": model, "to_pct": pct, "why": why})
    for pid, (active, why) in C.STATUS_DECISIONS.items():
        row = conn.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
        if row and bool(row["active"]) != bool(active):
            actions["status"].append({"property_id": pid, "from": row["active"], "to": active, "why": why})
    for pid, (date, why) in C.START_DATES.items():
        row = conn.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
        if row and row["start_date"] != date:
            actions["start_dates"].append({"property_id": pid, "from": row["start_date"], "to": date, "why": why})
    for period, label, amount, why, category in C.CONFIRMED_DISTINCT:
        key = (" ".join(label.lower().split()), round(amount, 2))
        if not conn.execute("SELECT 1 FROM import_distinct WHERE period=? AND label_norm=? AND amount=?", (period, key[0], key[1])).fetchone():
            actions["distinct"].append({"period": period, "label": label, "label_norm": key[0], "amount": key[1], "why": why})
    actions["pre_start"], actions["pre_start_left"] = pre_start_copies(conn)
    for d in echo_duplicates(conn):
        (actions["duplicates"] if d["match"] else actions["left_alone"]).append(d)
    return actions


def apply_cleanup(conn, actions, user="owner", note="property names, NW4 model, confirmed duplicate business rows"):
    """Apply a plan_cleanup() result in one transaction, as a batch that undo_batch can reverse."""
    if not any(actions.get(k) for k in ("renames", "models", "duplicates", "start_dates", "distinct", "pre_start", "status")):
        return None                                           # nothing to do: no batch, no noise
    cur = conn.execute("INSERT INTO import_batches (filename, file_hash, status, kind, note) VALUES (?,?,?,?,?)",
                       ("cleanup: " + note, "-", "applied", "cleanup", note))
    bid = cur.lastrowid
    removed = 0
    try:
        for a in actions["renames"]:
            row = conn.execute("SELECT * FROM properties WHERE id=?", (a["property_id"],)).fetchone()
            old = {"name": row["name"], "address": row["address"]}
            new_address = a["to"]                      # the canonical full name IS the address
            conn.execute("UPDATE properties SET name=?, address=? WHERE id=?", (a["to"], new_address, a["property_id"]))
            _log(conn, bid, "property_updated", "properties", None, {"id": a["property_id"], "old": old, "new": {"name": a["to"], "address": new_address}})
            for alias, kind in ((row["name"], "previous_name"), (a["to"], "name")):
                if identity.add_alias(conn, a["property_id"], alias, kind):
                    _log(conn, bid, "alias_added", "property_identity_aliases", None, {"alias_norm": identity.norm(alias)})
        for a in actions.get("status", []):
            conn.execute("UPDATE properties SET active=? WHERE id=?", (a["to"], a["property_id"]))
            _log(conn, bid, "property_updated", "properties", None, {"id": a["property_id"], "old": {"active": a["from"]}, "new": {"active": a["to"]}})
        for a in actions["start_dates"]:
            conn.execute("UPDATE properties SET start_date=? WHERE id=?", (a["to"], a["property_id"]))
            _log(conn, bid, "property_updated", "properties", None, {"id": a["property_id"], "old": {"start_date": a["from"]}, "new": {"start_date": a["to"]}})
        for a in actions["distinct"]:
            conn.execute("INSERT OR IGNORE INTO import_distinct (period, label_norm, amount, label, note, batch_id) VALUES (?,?,?,?,?,?)",
                         (a["period"], a["label_norm"], a["amount"], a["label"], a["why"], bid))
            _log(conn, bid, "distinct_added", "import_distinct", None, {"period": a["period"], "label_norm": a["label_norm"], "amount": a["amount"]})
        for a in actions["models"]:
            new_pct = None if a["to"] == "operated" else a.get("to_pct")
            new_managed = 0 if a["to"] == "operated" else 1
            new_pct = None if a["to"] == "operated" else a.get("to_pct")
            conn.execute("UPDATE properties SET management_fee_pct=?, is_managed=? WHERE id=?", (new_pct, new_managed, a["property_id"]))
            _log(conn, bid, "property_updated", "properties", None, {"id": a["property_id"], "old": {"management_fee_pct": a["from_pct"], "is_managed": a["from_is_managed"]},
                                                                      "new": {"management_fee_pct": new_pct, "is_managed": new_managed}})
        for d in actions["duplicates"] + actions["pre_start"]:
            row = conn.execute("SELECT * FROM transactions WHERE id=?", (d["row"]["id"],)).fetchone()
            if row is None:
                continue
            _log(conn, bid, "removed", "transactions", row["id"], dict(row))
            conn.execute("DELETE FROM transactions WHERE id=?", (row["id"],))
            removed += 1
        conn.execute("UPDATE import_batches SET properties=?, row_count=?, rows_removed=?, applied_at=datetime('now') WHERE id=?",
                     (json.dumps(sorted({a["property_id"] for a in actions["renames"] + actions["models"] + actions["start_dates"] + actions.get("status", [])})),
                      removed + len(actions["renames"]) + len(actions["models"]) + len(actions["start_dates"]) + len(actions["distinct"]) + len(actions.get("status", [])), removed, bid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return bid
