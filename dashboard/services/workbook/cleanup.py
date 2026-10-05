"""One-off, reviewable data corrections: full display names, a wrong property model, and confirmed double-counted
business rows. Everything is written through a batch (kind='cleanup') so it can be undone exactly, and nothing is
deleted unless it is a CONFIRMED duplicate. Dry-run first (`plan_cleanup`), then `apply_cleanup`."""
import json

from . import config as C
from . import identity
from . import plan as P
from .apply import _log


def _model(row):
    return "managed" if row["management_fee_pct"] else "operated"


def mapping_table(conn, year=2026):
    """CURRENT NAME / CANONICAL FULL NAME / SHORT CODE / WORKBOOK SHEET / MODEL / ALIASES / CONFIDENCE / SOURCE OF NAME."""
    rows = []
    code_of = {pid: code for code, (pid, _n) in C.PROPERTY_SHEETS.items()}
    for pid, (name, conf, source, question) in C.CANONICAL_NAMES.items():
        row = conn.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
        code = code_of.get(pid, "")
        model = _model(row) if row else "not in dashboard"
        decision = C.MODEL_DECISIONS.get(pid)
        if row and decision and decision[0] != model:
            model = f"{model} -> {decision[0]}"
        aliases = sorted({code, name, *(row["name"] for _ in [0] if row), *C.PROPERTY_ALIASES.get(code, [])} - {""})
        rows.append({"property_id": pid, "current_name": row["name"] if row else "(not in the dashboard)", "canonical": name, "code": code,
                     "sheet": f"{code}{year % 100:02d}" if code else "", "model": model, "aliases": aliases, "confidence": conf,
                     "source": source, "question": question})
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
        conn.execute("UPDATE properties SET management_fee_pct=? WHERE id=?", (new_pct, pid))
        after = {m: P.dashboard_view(conn, pid, m) for m in months}
    return [{"month": m, "before": before[m], "after": after[m]} for m in months]


def plan_cleanup(conn):
    actions = {"renames": [], "models": [], "duplicates": [], "left_alone": [], "flags": []}
    for pid, (name, conf, source, question) in C.CANONICAL_NAMES.items():
        row = conn.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
        if not row:
            continue
        if row["name"] != name:
            actions["renames"].append({"property_id": pid, "from": row["name"], "to": name, "address_follows": row["address"] == row["name"]})
        if question:
            actions["flags"].append({"property_id": pid, "question": question})
    for pid, (model, why) in C.MODEL_DECISIONS.items():
        row = conn.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
        if row and _model(row) != model:
            actions["models"].append({"property_id": pid, "from": _model(row), "from_pct": row["management_fee_pct"], "to": model, "why": why})
    for d in echo_duplicates(conn):
        (actions["duplicates"] if d["match"] else actions["left_alone"]).append(d)
    return actions


def apply_cleanup(conn, actions, user="owner", note="property names, NW4 model, confirmed duplicate business rows"):
    """Apply a plan_cleanup() result in one transaction, as a batch that undo_batch can reverse."""
    cur = conn.execute("INSERT INTO import_batches (filename, file_hash, status, kind, note) VALUES (?,?,?,?,?)",
                       ("cleanup: " + note, "-", "applied", "cleanup", note))
    bid = cur.lastrowid
    removed = 0
    try:
        for a in actions["renames"]:
            row = conn.execute("SELECT * FROM properties WHERE id=?", (a["property_id"],)).fetchone()
            old = {"name": row["name"], "address": row["address"]}
            new_address = a["to"] if a["address_follows"] else row["address"]
            conn.execute("UPDATE properties SET name=?, address=? WHERE id=?", (a["to"], new_address, a["property_id"]))
            _log(conn, bid, "property_updated", "properties", None, {"id": a["property_id"], "old": old, "new": {"name": a["to"], "address": new_address}})
            for alias, kind in ((row["name"], "previous_name"), (a["to"], "name")):
                if identity.add_alias(conn, a["property_id"], alias, kind):
                    _log(conn, bid, "alias_added", "property_identity_aliases", None, {"alias_norm": identity.norm(alias)})
        for a in actions["models"]:
            new_pct = None if a["to"] == "operated" else a.get("to_pct")
            conn.execute("UPDATE properties SET management_fee_pct=? WHERE id=?", (new_pct, a["property_id"]))
            _log(conn, bid, "property_updated", "properties", None, {"id": a["property_id"], "old": {"management_fee_pct": a["from_pct"]}, "new": {"management_fee_pct": new_pct}})
        for d in actions["duplicates"]:
            row = conn.execute("SELECT * FROM transactions WHERE id=?", (d["row"]["id"],)).fetchone()
            if row is None:
                continue
            _log(conn, bid, "removed", "transactions", row["id"], dict(row))
            conn.execute("DELETE FROM transactions WHERE id=?", (row["id"],))
            removed += 1
        conn.execute("UPDATE import_batches SET properties=?, row_count=?, rows_removed=?, applied_at=datetime('now') WHERE id=?",
                     (json.dumps(sorted({a["property_id"] for a in actions["renames"] + actions["models"]})), removed + len(actions["renames"]) + len(actions["models"]), removed, bid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return bid
