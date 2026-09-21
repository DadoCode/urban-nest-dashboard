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


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def find_duplicate(conn, property_id, item):
    """Returns the matching transaction's id, or None -- callers that just
    want a boolean can check truthiness."""
    if not property_id or not item.get("amount"):
        return None
    date = item.get("date") or ""
    year_month = date[:7] if re.match(r"^\d{4}-\d{2}", date) else None
    if not year_month:
        return None
    start, end = kpis.month_bounds(*map(int, year_month.split("-")))
    row = conn.execute(
        """SELECT id FROM transactions WHERE property_id=? AND date>=? AND date<?
           AND ABS(amount - ?) < 0.01 AND (vendor = ? OR description = ?) LIMIT 1""",
        (property_id, start, end, item["amount"], item.get("vendor"), item.get("description")),
    ).fetchone()
    return row["id"] if row else None


def find_duplicate_reservation(conn, property_id, item):
    """A reservation already on file for this flat: same confirmation code,
    or the same check-in/check-out dates. Returns the booking id or None."""
    if not property_id:
        return None
    code = (item.get("reservation_id") or "").strip()
    if code:
        row = conn.execute("SELECT id FROM bookings WHERE property_id=? AND reservation_id=? LIMIT 1", (property_id, code)).fetchone()
        if row:
            return row["id"]
    row = conn.execute(
        """SELECT id FROM bookings WHERE property_id=? AND check_in=? AND check_out=?
           AND reservation_id != 'monthly-aggregate' AND status='confirmed' LIMIT 1""",
        (property_id, item.get("check_in"), item.get("check_out")),
    ).fetchone()
    return row["id"] if row else None


def save_upload(conn, file, doc_type, property_id, flash):
    """Handles a single uploaded file: saves it, extracts line items into
    document_items (the reviewable Document -> extracted -> reviewed ->
    transaction lineage -- see db.py's schema comment), guesses a property
    when none was given, flags likely duplicates. `flash` is Flask's
    flash() (passed in so this stays framework-agnostic about how a route
    reports back to the user). Returns the new document id."""
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

    result = extraction.extract(dest_path, mime_type, doc_type)
    if result is None:
        conn.execute("UPDATE documents SET status='failed' WHERE id=?", (doc_id,))
        conn.commit()
        flash(f"Saved {file.filename}. We couldn't read this file automatically -- add its line items by hand below. "
              f"(No extraction available for this file type, or no API key configured.)")
        return doc_id

    items = result["items"]
    detected_property_id = property_id
    if not detected_property_id and result.get("document_hint"):
        properties = [dict(p) for p in get_properties(conn, include_overhead=False)]
        guessed_id, confidence = extraction.guess_property(result["document_hint"], properties)
        if guessed_id:
            detected_property_id = guessed_id

    period = period_from_hint(result.get("period_hint"))
    is_reservations = result.get("kind") == "reservation"
    properties = [dict(p) for p in get_properties(conn, include_overhead=False)]
    dupes = 0
    for i, item in enumerate(items):
        if is_reservations:
            # a statement covers several flats -- match each reservation to its own by listing name
            item_property = property_id
            if not item_property and item.get("description"):
                item_property = extraction.guess_property(item["description"], properties)[0]
            item_property = item_property or detected_property_id
            duplicate_of = find_duplicate_reservation(conn, item_property, item)
            dupes += bool(duplicate_of)
            platform = item.get("platform") or result.get("platform")
            conn.execute(
                """INSERT INTO document_items (document_id, line_index, raw_description, date, vendor, amount,
                       direction, property_id, category, capex, confidence, include, original_extracted_value,
                       item_kind, check_in, check_out, reservation_id, platform, gross_revenue, platform_fees, net_revenue,
                       source_page, source_row)
                   VALUES (?,?,?,?,?,?,'income',?,'booking_income',0,?,?,?,'reservation',?,?,?,?,?,?,?,?,?)""",
                (doc_id, i, item.get("description"), item.get("check_in"), platform, item.get("net"),
                 item_property, item.get("confidence"), 0 if duplicate_of else 1, json.dumps(item),
                 item.get("check_in"), item.get("check_out"), item.get("reservation_id"), platform,
                 item.get("gross"), item.get("fees"), item.get("net"), _int(item.get("page")), _int(item.get("row"))),
            )
            continue
        item["possible_duplicate"] = None
        duplicate_of = find_duplicate(conn, detected_property_id, item)
        dupes += bool(duplicate_of)
        category = item.get("category") or "other"
        direction = "income" if category == "booking_income" else "expense"
        conn.execute(
            """INSERT INTO document_items (document_id, line_index, raw_description, date, vendor, amount,
                   direction, property_id, category, capex, confidence, duplicate_of, original_extracted_value,
                   source_page, source_row)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, i, item.get("description"), item.get("date"), item.get("vendor"), item.get("amount"),
             direction, detected_property_id, category, 1 if category == "furniture" else 0, item.get("confidence"),
             duplicate_of, json.dumps(item), _int(item.get("page")), _int(item.get("row"))),
        )

    conn.execute(
        """UPDATE documents SET status='extracted', extracted_json=?, property_id=?,
             detected_year=?, detected_month=? WHERE id=?""",
        (json.dumps(items), detected_property_id, period[0] if period else None,
         period[1] if period else None, doc_id),
    )
    conn.commit()
    noun = "reservation" if is_reservations else "line item"
    msg = f"Extracted {len(items)} {noun}(s) from {file.filename} — review and confirm below."
    if dupes:
        msg += f" {dupes} look like they might already be on file" + (" (unticked so they aren't counted twice)." if is_reservations else ".")
    flash(msg)
    return doc_id
