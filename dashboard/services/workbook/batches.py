"""Import batches: stage an uploaded workbook, report on it, find provenance.

Only the PARSED content of the allowed sheets is stored with a batch -- never the
uploaded file -- so nothing from a sheet the importer does not read (the credential
sheet) is ever written to disk.
"""
import json

from . import config as C
from . import identity
from . import plan as P
from . import reader


def stage(conn, data, filename):
    """Parse an uploaded workbook and keep the result as a 'staged' batch. Returns the batch id."""
    parsed = reader.parse_workbook(data, filename, set(identity.mapping(conn)))
    report = validation_report(parsed, conn)
    cur = conn.execute(
        """INSERT INTO import_batches (filename, file_hash, workbook_year, status, parsed, validation)
           VALUES (?,?,?,?,?,?)""",
        (filename, parsed["sha256"], parsed["year"], "staged", json.dumps(parsed, default=str), json.dumps(report)))
    conn.commit()
    return cur.lastrowid


def load(conn, batch_id):
    row = conn.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        return None, None
    return row, (json.loads(row["parsed"]) if row["parsed"] else None)


def cancel(conn, batch_id):
    conn.execute("UPDATE import_batches SET status='cancelled' WHERE id=? AND status='staged'", (batch_id,))
    conn.commit()


def validation_report(parsed, conn=None):
    """What was found in the workbook, independent of any month."""
    roles = parsed["roles"]
    used = [n for n, (r, _d) in roles.items() if r in ("main", "breakdown", "property", "candidate")]
    ignored = [{"sheet": n, "reason": d} for n, (r, d) in roles.items() if r == "ignored"]
    blocked = [n for n, (r, _d) in roles.items() if r == "blocked"]
    candidates = [n for n, (r, _d) in roles.items() if r == "candidate"]
    year = parsed["year"]
    yy = f"{year % 100:02d}" if year else "??"
    required = [{"name": f"{C.MAIN_SHEET_BASE}{yy}", "ok": any(r == "main" for r, _d in roles.values())},
                {"name": f"{C.BREAKDOWN_SHEET_BASE}{yy}", "ok": any(r == "breakdown" for r, _d in roles.values()),
                 "note": "purchase detail; without it the sheets' 'Purchases' lumps are imported instead"}]
    missing = []
    mapped = identity.mapping(conn) if conn is not None else {c: {"name": n} for c, (_p, n) in C.PROPERTY_SHEETS.items()}
    for code, info in mapped.items():
        if code not in parsed["properties"] and not info.get("main_only"):
            missing.append({"sheet": f"{code}{yy}", "property": info["name"]})
    names = [n.strip().lower() for n in roles]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    issues = {"error": [], "review": [], "info": []}
    for i in parsed["issues"]:
        issues[i["level"]].append(i)
    if duplicates:
        issues["error"].append({"level": "error", "code": "duplicate_sheet", "scope": None, "month": None, "ref": None,
                                "message": "Duplicate sheet names: " + ", ".join(duplicates)})
    unmapped_msgs = [{"sheet": n, "message": "New property detected -- review and confirm it in the preview before anything is created."} for n in candidates]
    global_errors = [i for i in issues["error"] if i["code"] in ("no_year", "main_layout", "duplicate_sheet")]
    return {"year": year, "required": required, "used": used, "ignored": ignored, "blocked": blocked,
            "unmapped": unmapped_msgs, "missing_properties": missing, "issues": issues,
            "can_import": not global_errors and year is not None,
            "global_errors": [i["message"] for i in global_errors]}


def provenance(conn, property_id, ym):
    """The latest applied batch that wrote this property-month, or None."""
    for r in conn.execute("SELECT id, filename, applied_at, properties FROM import_batches WHERE status='applied' AND period=? ORDER BY id DESC", (ym,)):
        if property_id in json.loads(r["properties"] or "[]"):
            return {"id": r["id"], "filename": r["filename"], "applied_at": r["applied_at"]}
    return None


def provenance_for_range(conn, property_id, start_ym, end_ym):
    """The most recent applied import that wrote any month in [start_ym, end_ym] for this property (or any property).
    Quiet by design: a demo database that predates the import tables simply has none."""
    try:
        rows = conn.execute("SELECT id, filename, applied_at, period, properties FROM import_batches WHERE status='applied' "
                            "AND period>=? AND period<=? ORDER BY id DESC", (start_ym, end_ym)).fetchall()
    except Exception:
        return None
    for r in rows:
        if property_id is None or property_id in json.loads(r["properties"] or "[]"):
            return {"id": r["id"], "filename": r["filename"], "applied_at": r["applied_at"], "period": r["period"]}
    return None


def list_batches(conn, status="", sort="uploaded", direction="desc"):
    order = {"uploaded": "uploaded_at", "period": "COALESCE(period,'')", "status": "status"}.get(sort, "uploaded_at")
    d = "ASC" if direction == "asc" else "DESC"
    where, params = ("WHERE status=?", (status,)) if status else ("WHERE status!='cancelled'", ())
    return [dict(r) for r in conn.execute(
        f"""SELECT id, filename, uploaded_at, workbook_year, period, status, applied_at, undone_at, properties, row_count
            FROM import_batches {where} ORDER BY {order} {d}, id DESC""", params)]
