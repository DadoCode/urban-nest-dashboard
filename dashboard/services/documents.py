"""Document-inbox pipeline helpers: saving an upload, running extraction,
and flagging likely duplicates against the existing ledger. Pulled out of
the documents route module so the upload/extract/dedupe logic isn't tied
to any one Flask view."""
import datetime
import json
import mimetypes
import re
from pathlib import Path

import services.extraction as extraction
import services.kpis as kpis
from services.common import get_properties

ROOT = Path(__file__).resolve().parent.parent.parent
UPLOADS = ROOT / "data" / "uploads"


def period_from_hint(hint):
    if hint and re.match(r"^\d{4}-\d{2}$", hint):
        y, m = hint.split("-")
        return int(y), int(m)
    return None


def find_duplicate(conn, property_id, item):
    if not property_id or not item.get("amount"):
        return False
    date = item.get("date") or ""
    year_month = date[:7] if re.match(r"^\d{4}-\d{2}", date) else None
    if not year_month:
        return False
    start, end = kpis.month_bounds(*map(int, year_month.split("-")))
    row = conn.execute(
        """SELECT 1 FROM transactions WHERE property_id=? AND date>=? AND date<?
           AND ABS(amount - ?) < 0.01 AND (vendor = ? OR description = ?) LIMIT 1""",
        (property_id, start, end, item["amount"], item.get("vendor"), item.get("description")),
    ).fetchone()
    return bool(row)


def save_upload(conn, file, doc_type, property_id, flash):
    """Handles a single uploaded file: saves it, extracts line items,
    guesses a property when none was given, flags likely duplicates.
    `flash` is Flask's flash() (passed in so this stays framework-agnostic
    about how a route reports back to the user). Returns the new document id."""
    dest_dir = UPLOADS / (property_id or "_unassigned")
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{Path(file.filename).name}"
    dest_path = dest_dir / safe_name
    file.save(dest_path)

    mime_type = file.mimetype or mimetypes.guess_type(file.filename)[0]
    cur = conn.execute(
        "INSERT INTO documents (property_id, filename, stored_path, doc_type, status) VALUES (?,?,?,?,'pending')",
        (property_id, file.filename, str(dest_path), doc_type),
    )
    doc_id = cur.lastrowid
    conn.commit()

    result = extraction.extract(dest_path, mime_type)
    if result is None:
        flash(f"Saved {file.filename}. No extraction available for this file type/no API key, so add its line items by hand below.")
        return doc_id

    items = result["items"]
    detected_property_id = property_id
    if not detected_property_id and result.get("document_hint"):
        properties = [dict(p) for p in get_properties(conn, include_overhead=False)]
        guessed_id, confidence = extraction.guess_property(result["document_hint"], properties)
        if guessed_id:
            detected_property_id = guessed_id

    period = period_from_hint(result.get("period_hint"))
    for item in items:
        item["possible_duplicate"] = find_duplicate(conn, detected_property_id, item)

    conn.execute(
        """UPDATE documents SET status='extracted', extracted_json=?, property_id=?,
             detected_year=?, detected_month=? WHERE id=?""",
        (json.dumps(items), detected_property_id, period[0] if period else None,
         period[1] if period else None, doc_id),
    )
    conn.commit()
    dupes = sum(1 for i in items if i["possible_duplicate"])
    msg = f"Extracted {len(items)} line item(s) from {file.filename} — review and confirm below."
    if dupes:
        msg += f" {dupes} look like they might already be on file."
    flash(msg)
    return doc_id
