"""Document-inbox pipeline helpers: saving an upload, running extraction,
and flagging likely duplicates against the existing ledger. Pulled out of
the documents route module so the upload/extract/dedupe logic isn't tied
to any one Flask view."""
import datetime
import json
import mimetypes
import re
from collections import Counter
from pathlib import Path

from werkzeug.utils import secure_filename

import services.extraction as extraction
import services.ingest as ingest
import services.kpis as kpis
from services import runtime
from services.common import get_properties

# What the pipeline knows how to open. Anything else is still kept (so the
# attempt is visible and auditable) but marked Failed with a clear reason.
SPREADSHEET_EXTS = {".csv", ".tsv", ".xls", ".xlsx", ".xlsm"}
SUPPORTED_EXTS = SPREADSHEET_EXTS | {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt", ".docx"}
LOW_CONFIDENCE = 0.7


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


def _size_label(n):
    if n < 1024:
        return f"{n} bytes"
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB"


def _fail(conn, doc_id, filename, code, message, flash, warnings=None, dup=None):
    """Mark a document Failed with a plain-words reason. The file stays on
    disk and the trail records why, so the manual-entry fallback still works."""
    detection = {"warnings": (warnings or [])}
    conn.execute(
        "UPDATE documents SET status='failed', failure_reason=?, detection_json=?, duplicate_of_document=? WHERE id=?",
        (message, json.dumps(detection), dup["id"] if dup else None, doc_id))
    ingest.log_event(conn, doc_id, "extraction_failed", message, {"code": code})
    conn.commit()
    flash(f"{filename}: {message}", "warning")
    return doc_id


def save_upload(conn, file, doc_type, property_id, flash):
    """Handles a single uploaded file: saves it, extracts line items into
    document_items (the reviewable Document -> extracted -> reviewed ->
    transaction lineage -- see db.py's schema comment), detects property
    and period (saying so when unsure), and flags likely duplicates. Every
    step is written to the document's trail. `flash` is Flask's flash()
    (passed in so this stays framework-agnostic about how a route reports
    back). Returns the new document id, or None if the file couldn't even
    be stored (nothing is created in that case)."""
    original = Path(file.filename).name
    ext = Path(original).suffix.lower()
    if property_id and not conn.execute("SELECT 1 FROM properties WHERE id=?", (property_id,)).fetchone():
        property_id = None

    dest_dir = runtime.uploads_dir() / (property_id or "_unassigned")
    stored_name = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}_{secure_filename(original) or 'upload'}"
    dest_path = dest_dir / stored_name
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        file.save(dest_path)
    except OSError:
        flash(f"{original}: we couldn't store this file, so nothing was uploaded. Check that the uploads folder is writable and has space.", "error")
        return None

    size = dest_path.stat().st_size
    file_hash = ingest.file_sha256(dest_path)
    mime_type = file.mimetype or mimetypes.guess_type(original)[0]
    cur = conn.execute(
        "INSERT INTO documents (property_id, filename, stored_path, doc_type, status, file_hash, file_size) VALUES (?,?,?,?,'processing',?,?)",
        (property_id, original, str(dest_path), doc_type, file_hash, size),
    )
    doc_id = cur.lastrowid
    ingest.log_event(conn, doc_id, "uploaded", f"Uploaded {original} ({_size_label(size)}) as {doc_type.replace('_', ' ')}",
                     {"filename": original, "size": size, "doc_type": doc_type, "property_selected": property_id})
    conn.commit()

    dup_doc = ingest.find_duplicate_document(conn, doc_id, file_hash)
    warnings = []
    if dup_doc:
        warnings.append({"code": "duplicate_file", "level": "warn",
                         "message": f"Possible duplicate: this exact file was already uploaded on {dup_doc['uploaded_at'][:10]} as \u201c{dup_doc['filename']}\u201d ({ingest.status_display(dup_doc['status'])[0].lower()}).",
                         "document_id": dup_doc["id"]})

    if size == 0:
        return _fail(conn, doc_id, original, "empty_file", "This file is empty (0 bytes). Check the original and upload it again.", flash, warnings, dup_doc)
    if ext not in SUPPORTED_EXTS:
        hint = " HEIC photos need exporting as JPG first." if ext in (".heic", ".heif") else ""
        return _fail(conn, doc_id, original, "unsupported_type",
                     f"{ext or 'This'} files aren't supported.{hint} Upload a PDF, a JPG/PNG photo, an Excel/CSV file, or a Word/text document.", flash, warnings, dup_doc)

    result, failure = extraction.extract_with_reason(dest_path, mime_type, doc_type)
    if failure:
        return _fail(conn, doc_id, original, failure["code"], failure["message"], flash, warnings, dup_doc)

    items = result["items"]
    if not items:
        return _fail(conn, doc_id, original, "no_rows",
                     "No lines could be read from this document. If it's a scan or photo it may be too blurry, cropped or empty — try a clearer copy, or enter the rows manually.",
                     flash, warnings, dup_doc)

    properties = [dict(p) for p in get_properties(conn, include_overhead=False)]
    names = {p["id"]: p["name"] for p in properties}
    is_reservations = result.get("kind") == "reservation"
    overhead = conn.execute("SELECT id, name FROM properties WHERE type='overhead' LIMIT 1").fetchone()
    names_all = {**names, **({overhead["id"]: overhead["name"]} if overhead else {})}
    warnings += result.get("notes", [])

    # ---- Amazon Business: the PO Number says which property each line was bought for ----
    po_lines = {"business": 0, "exclude": 0}
    po_problems = Counter()
    if result.get("source_kind") == "amazon_business" and not property_id:  # a property chosen at upload wins over POs
        for item in items:
            pid, action, note = extraction.resolve_po(item.get("po"), properties, overhead["id"] if overhead else None)
            item["_property_id"] = pid
            item["_exclude"] = action == "exclude"
            item["_po_note"] = note
            if action in po_lines:
                po_lines[action] += 1
            elif action in ("ambiguous", "unknown", "shared", "none"):
                po_problems[(action, item.get("po") or "")] += 1
        if po_lines["exclude"]:
            warnings.append({"code": "po_personal", "level": "info",
                             "message": f"{po_lines['exclude']} line{'s' if po_lines['exclude'] != 1 else ''} have PO \u201cpersonal\u201d and were left unticked so they don't reach your figures. Tick them if they're business costs."})
        if po_lines["business"]:
            warnings.append({"code": "po_business", "level": "info",
                             "message": f"{po_lines['business']} line{'s' if po_lines['business'] != 1 else ''} with PO \u201cbiz\u201d were assigned to {overhead['name']} (a Business Cost, not a property cost)."})
        if po_problems:
            total_problem = sum(po_problems.values())
            examples = "; ".join(f"\u201c{po or '(blank)'}\u201d \u00d7{n}" for (_a, po), n in po_problems.most_common(4))
            warnings.append({"code": "po_unmatched", "level": "warn",
                             "message": f"{total_problem} line{'s' if total_problem != 1 else ''} have a PO Number that isn't one property of yours ({examples}). Choose the property on each (or exclude it)."})

    # ---- which property? (chosen by you > document text > file name; two near-equal matches = ambiguous) ----
    prop_info = {"source": "none", "id": None, "candidates": []}
    detected_property_id = property_id
    if result.get("source_kind") == "amazon_business" and not property_id:
        distinct = {i["_property_id"] for i in items}
        prop_info["source"] = "PO number on each line"
        if len(distinct) == 1 and None not in distinct:
            detected_property_id = next(iter(distinct))
            prop_info["id"] = detected_property_id
    elif property_id:
        prop_info.update(source="selected by you", id=property_id)
    else:
        hint_text, source = result.get("document_hint"), "text on the document"
        ranked = extraction.rank_properties(hint_text, properties)
        if not ranked:
            ranked, source = extraction.rank_properties(Path(original).stem, properties), "file name"
        prop_info["candidates"] = ranked[:3]
        if ranked and ingest.rank_is_ambiguous(ranked):
            warnings.append({"code": "property_ambiguous", "level": "warn",
                             "message": f"Could be {ranked[0]['name']} or {ranked[1]['name']} (the {source} matches both). No property was assigned — choose one before confirming."})
        elif ranked:
            detected_property_id = ranked[0]["id"]
            prop_info.update(source=source, id=ranked[0]["id"], confidence=ranked[0]["score"])
        elif not is_reservations:
            warnings.append({"code": "property_missing", "level": "warn",
                             "message": "No property was detected on this document. Choose one on each line before confirming."})

    # ---- which period? ----
    period, period_warnings = ingest.detect_period(items, is_reservations, result.get("period_hint"))
    warnings += period_warnings

    dupes_replace = dupes_kept_excel = overlap_lines = 0
    overlap_examples = {}
    no_property_lines, ignored_lines = 0, 0
    for i, item in enumerate(items):
        if is_reservations:
            # a statement covers several flats -- match each reservation to its own by listing name
            item_property, ignored = property_id, False
            if not item_property:
                for text in (item.get("description"), item.get("location")):
                    remembered = ingest.alias_lookup(conn, text)
                    if remembered:
                        item_property, ignored = remembered["property_id"], remembered["ignore"]
                        break
            if not item_property and not ignored:
                for text in (item.get("description"), item.get("location")):
                    guess = extraction.confident_property(text, properties) if text else None
                    if guess:
                        item_property = guess
                        break
            if not ignored:
                item_property = item_property or detected_property_id
            ignored_lines += ignored
            no_property_lines += not item_property and not ignored
            duplicate_of = find_duplicate_reservation(conn, item_property, item)
            dup_default = ingest.duplicate_default(ingest.source_of(conn, "bookings", duplicate_of)) if duplicate_of else None
            dupes_replace += dup_default == "replace"
            dupes_kept_excel += dup_default == "exclude"
            overlap = 0.0 if duplicate_of else ingest.excel_overlap(conn, item_property, item.get("check_in"))
            if overlap:  # Excel already has income for this property and month: adding on top could count the stay twice
                item["_excel_overlap"] = round(overlap, 2)
                overlap_lines += 1
                overlap_examples[(item_property, item["check_in"][:7])] = round(overlap, 2)
            start_ticked = not (dup_default == "exclude" or ignored or item.get("_exclude") or overlap)
            platform = item.get("platform") or result.get("platform")
            conn.execute(
                """INSERT INTO document_items (document_id, line_index, raw_description, date, vendor, amount,
                       direction, property_id, category, capex, confidence, include, original_extracted_value,
                       item_kind, check_in, check_out, reservation_id, platform, gross_revenue, platform_fees, net_revenue,
                       source_page, source_row)
                   VALUES (?,?,?,?,?,?,'income',?,'booking_income',0,?,?,?,'reservation',?,?,?,?,?,?,?,?,?)""",
                (doc_id, i, item.get("description"), item.get("check_in"), platform, item.get("net"),
                 item_property, item.get("confidence"), 1 if start_ticked else 0, json.dumps(item),
                 item.get("check_in"), item.get("check_out"), item.get("reservation_id"), platform,
                 item.get("gross"), item.get("fees"), item.get("net"), _int(item.get("page")), _int(item.get("row"))),
            )
            continue
        item["possible_duplicate"] = None
        line_property = item["_property_id"] if "_property_id" in item else detected_property_id
        duplicate_of = find_duplicate(conn, line_property, item)
        dup_default = ingest.duplicate_default(ingest.source_of(conn, "transactions", duplicate_of)) if duplicate_of else None
        dupes_replace += dup_default == "replace"
        dupes_kept_excel += dup_default == "exclude"
        category = item.get("category") or "other"
        direction = "income" if category == "booking_income" else "expense"
        conn.execute(
            """INSERT INTO document_items (document_id, line_index, raw_description, date, vendor, amount,
                   direction, property_id, category, capex, confidence, duplicate_of, original_extracted_value,
                   source_page, source_row, include)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, i, item.get("description"), item.get("date"), item.get("vendor"), item.get("amount"),
             direction, line_property, category, 1 if category == "furniture" else 0, item.get("confidence"),
             duplicate_of, json.dumps(item), _int(item.get("page")), _int(item.get("row")), 0 if (item.get("_exclude") or dup_default == "exclude") else 1),
        )
    if ignored_lines:
        warnings.append({"code": "listings_ignored", "level": "info",
                         "message": f"{ignored_lines} reservation{'s' if ignored_lines != 1 else ''} belong to listings you marked as not tracked; left unticked."})
    if is_reservations and no_property_lines:
        warnings.append({"code": "property_missing", "level": "warn",
                         "message": f"{no_property_lines} of {len(items)} reservations couldn't be matched to a property. Choose one on each before confirming."})

    # ---- partial extraction: what the parser skipped or wasn't sure about ----
    amount_key = "net" if is_reservations else "amount"
    total = round(sum(abs(float(i.get(amount_key) or 0)) for i in items), 2)
    confs = [float(i["confidence"]) for i in items if i.get("confidence") is not None]
    low = sum(1 for c in confs if c < LOW_CONFIDENCE)
    gaps = []
    skipped = (result.get("rows_seen") or 0) - len(items)
    if skipped > 0:
        gaps.append(f"{skipped} of {result['rows_seen']} rows in the file couldn't be read and were skipped")
    missing = sum(1 for i in items if i.get(amount_key) is None and not i.get("_exclude"))
    if missing:
        gaps.append(f"{missing} line{'s' if missing != 1 else ''} without an amount")
    if not is_reservations:
        undated = sum(1 for i in items if not i.get("date"))
        if undated:
            gaps.append(f"{undated} line{'s' if undated != 1 else ''} without a date")
    if low:
        gaps.append(f"{low} line{'s' if low != 1 else ''} below {int(LOW_CONFIDENCE * 100)}% confidence")
    if gaps:
        warnings.append({"code": "partial_extraction", "level": "warn", "message": "Partial extraction: " + "; ".join(gaps) + ". Check these against the source."})

    if not is_reservations and doc_type != "booking_statement":
        # The spreadsheet reader treats positive amounts as money in unless the document type says otherwise.
        # Say so, rather than letting a list of costs quietly default to income.
        n_income = sum(1 for i in items if (i.get("category") or "other") == "booking_income")
        if n_income:
            warnings.append({"code": "income_guess", "level": "warn",
                             "message": f"{n_income} line{'s were' if n_income != 1 else ' was'} categorised as Booking Income (money in). If this document lists costs, change each line's category before confirming."})

    year = month = None
    if period:
        year, month = map(int, (period.get("dominant") or period["from"]).split("-"))

    similar = None if dup_doc else ingest.find_similar_document(conn, doc_id, doc_type, detected_property_id, year, month, len(items), total)
    if similar:
        warnings.append({"code": "duplicate_content", "level": "warn",
                         "message": f"Possible duplicate: \u201c{similar['filename']}\u201d (uploaded {similar['uploaded_at'][:10]}) is the same kind of document for the same property and period with the same number of lines and total.",
                         "document_id": similar["id"]})
    noun_dup = "reservation" if is_reservations else "line"
    if dupes_replace:
        warnings.append({"code": "duplicate_rows", "level": "warn",
                         "message": f"{dupes_replace} {noun_dup}{'s' if dupes_replace != 1 else ''} already exist from an earlier upload and will REPLACE that earlier version when you confirm. Change a line to \u201cKeep both\u201d if it is genuinely separate."})
    if dupes_kept_excel:
        warnings.append({"code": "duplicate_excel", "level": "info",
                         "message": f"{dupes_kept_excel} {noun_dup}{'s' if dupes_kept_excel != 1 else ''} match records from your Excel history. Excel is kept untouched, so these are left out (unticked)."})
    if overlap_lines:
        shown = "; ".join(f"{names_all.get(p, p)} {ingest.MONTH_ABBR[int(ym[5:])]} {ym[:4]} (Excel £{amt:,.0f})" for (p, ym), amt in list(overlap_examples.items())[:3])
        warnings.append({"code": "excel_overlap", "level": "warn",
                         "message": f"{overlap_lines} reservation{'s' if overlap_lines != 1 else ''} fall in months where your Excel history already has income ({shown}{'…' if len(overlap_examples) > 3 else ''}). Excel is kept as it is, and it can't be matched stay by stay, so ticking these ADDS them on top and may count a stay twice. They start unticked; tick the ones that are genuinely new."})

    detection = {"property": prop_info, "period": period, "warnings": warnings,
                 "method": "spreadsheet parser" if ext in SPREADSHEET_EXTS else "AI extraction",
                 "total": total, "rows": len(items), "low_confidence": low}
    conn.execute(
        """UPDATE documents SET status='extracted', extracted_json=?, property_id=?, detected_year=?, detected_month=?,
             confidence=?, detection_json=?, duplicate_of_document=?, failure_reason=NULL WHERE id=?""",
        (json.dumps(items), detected_property_id, year, month,
         round(sum(confs) / len(confs), 3) if confs else None, json.dumps(detection),
         (dup_doc or similar)["id"] if (dup_doc or similar) else None, doc_id),
    )
    noun = "reservation" if is_reservations else "line"
    ingest.log_event(
        conn, doc_id, "extracted",
        f"Read {len(items)} {noun}{'s' if len(items) != 1 else ''} · total £{total:,.2f} · property: "
        f"{names.get(detected_property_id, 'not detected')} · period: {ingest.period_label(period) or 'not detected'}",
        {"rows": len(items), "total": total, "method": detection["method"], "property": prop_info, "period": period,
         "warnings": [w["code"] for w in warnings]})
    conn.commit()

    attention = sum(1 for w in warnings if w["level"] == "warn")
    msg = f"\u2713 Read {len(items)} {noun}{'s' if len(items) != 1 else ''} from {original} — check them below, then confirm."
    if attention:
        msg += f" {attention} thing{'s need' if attention != 1 else ' needs'} checking."
    flash(msg, "warning" if attention else "success")
    return doc_id
