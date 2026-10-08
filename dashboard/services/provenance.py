"""Where a recorded row came from, and whether the monthly workbook import vouches for it.

Read-only helpers for the drilldowns. Nothing here writes, and nothing here calculates a financial figure: it only
reads the `import_batch_id` / `source_ref` that the workbook import already stamped on each row it wrote, and the
reconciliation result the import already stored on its batch."""
import json

from services.common import MONTH_NAMES

MONTHS_IN = ("transactions", "bookings")


def month_label(ym):
    return f"{MONTH_NAMES[int(ym[5:7])]} {ym[:4]}" if ym else ""


def _file_label(filename):
    stem = (filename or "").rsplit(".", 1)[0]
    return stem.replace("_", " ").strip() or "Monthly workbook"


def is_workbook_row(row):
    """A row written by an applied monthly workbook import: individually read-only, because workbook = ledger = dashboard."""
    keys = row.keys()
    return bool(("import_batch_id" in keys and row["import_batch_id"]) or ("source" in keys and row["source"] == "workbook"))


def workbook_source(conn, row):
    """Provenance of a transactions/bookings row: {"kind": "workbook", ...} for a row a workbook import wrote,
    {"kind": "legacy", ...} for the older Excel history (which has no batch or cell to point at), else None."""
    keys = row.keys()
    batch_id = row["import_batch_id"] if "import_batch_id" in keys else None
    if batch_id:
        b = conn.execute("SELECT id, filename, period, applied_at, status FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if b:
            pid = row["property_id"]
            return {"kind": "workbook", "batch_id": b["id"], "label": _file_label(b["filename"]), "filename": b["filename"],
                    "period": b["period"], "period_label": month_label(b["period"]) if b["period"] else "",
                    "applied_at": (b["applied_at"] or "")[:16], "status": b["status"],
                    "ref": row["source_ref"] if "source_ref" in keys else None, "anchor": f"prop-{pid}", "property_id": pid}
    if "source" in keys and row["source"] == "excel_import":
        return {"kind": "legacy"}
    return None


def import_link(src_info):
    """URL path to the applied import page, anchored at the property's reconciliation block."""
    return f"/imports/{src_info['batch_id']}#{src_info['anchor']}"


def review_status(conn, property_id, start_ym, end_ym):
    """('REVIEW', batch_id) when the latest applied workbook import for any month in the range left this property in
    REVIEW (same rule the applied-import page uses: a failed check, or a blank control with money imported against it);
    ('PASS', batch_id) when it was imported cleanly; None when no workbook import covers it. The result is the one stored
    on the batch at apply time, so nothing is recalculated here."""
    seen = {}
    for b in conn.execute("SELECT id, period, properties, reconciliation FROM import_batches WHERE kind='workbook' AND status='applied' "
                          "AND period>=? AND period<=? ORDER BY id DESC", (start_ym, end_ym)):
        if b["period"] in seen:
            continue
        if property_id in json.loads(b["properties"] or "[]"):
            seen[b["period"]] = b
    verdict = None
    for ym in sorted(seen):
        checks = json.loads(seen[ym]["reconciliation"] or "{}").get(property_id, [])
        flagged = [c for c in checks if c["status"] == "REVIEW" or (c["status"] == "NO CONTROL" and c["imported"] not in (0, 0.0, None))]
        if flagged:
            return "REVIEW", seen[ym]["id"]
        verdict = verdict or ("PASS", seen[ym]["id"])
    return verdict
