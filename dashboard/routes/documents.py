import datetime
import json
from pathlib import Path

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

import db
import services.ingest as ingest
import services.reconcile as rc
from services import runtime
from services.audit import record, record_edits
from services.common import CATEGORIES, get_properties, get_property
import services.kpis as kpis
from services.common import MONTH_NAMES
import services.review as review_helpers
from services.documents import find_duplicate, find_duplicate_reservation, save_upload
from services.vendors import get_or_create_vendor

bp = Blueprint("documents", __name__)

DOC_TYPE_LABELS = ingest.DOC_TYPE_NAMES


def _period_text(doc):
    """'Feb 2026' / 'Jan–Mar 2026' for a document, or None when undetected."""
    period = ingest.detection_of(doc).get("period")
    if period:
        return ingest.period_label(period)
    if doc["detected_year"] and doc["detected_month"]:
        return f"{MONTH_NAMES[doc['detected_month']][:3]} {doc['detected_year']}"
    return None


@bp.route("/documents/<int:doc_id>/drawer")
def drawer(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        return "<p class='note'>Document not found.</p>", 404
    prop = get_property(conn, doc["property_id"])
    stats = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt, SUM(include) inc FROM document_items WHERE document_id=?", (doc_id,)).fetchone()
    label, kind = ingest.status_display(doc["status"])
    return render_template(
        "partials/document_drawer.html", doc=doc, prop=prop, stats=stats, status_label=label, status_kind=kind,
        type_label=DOC_TYPE_LABELS.get(doc["doc_type"], doc["doc_type"] or "Document"),
        period=_period_text(doc), warnings=ingest.detection_of(doc).get("warnings", []),
    )


@bp.route("/documents/<int:doc_id>/file")
def file(doc_id):
    doc = db.get_conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        abort(404)
    if not Path(doc["stored_path"]).is_file():
        return "The original file is no longer on disk (it may have been moved or deleted).", 404
    return send_file(doc["stored_path"], download_name=doc["filename"])


# Sort keys for the Documents list: stored as "<column>_<direction>".
_DOC_SORTS = {
    "uploaded_desc": "d.uploaded_at DESC, d.id DESC",
    "uploaded_asc": "d.uploaded_at ASC, d.id ASC",
    # documents with no detected period always sink to the bottom, either way
    "period_desc": "(d.detected_year IS NULL), d.detected_year DESC, d.detected_month DESC, d.uploaded_at DESC",
    "period_asc": "(d.detected_year IS NULL), d.detected_year ASC, d.detected_month ASC, d.uploaded_at DESC",
}


@bp.route("/documents")
def index():
    conn = db.get_conn()
    prefill_property = request.args.get("property") or ""
    prefill_type = request.args.get("type") if request.args.get("type") in DOC_TYPE_LABELS else ""
    f_property = request.args.get("d_property") or prefill_property
    f_type = request.args.get("d_type") or ""
    f_period = request.args.get("d_period") or ""
    sort = request.args.get("d_sort") if request.args.get("d_sort") in _DOC_SORTS else "uploaded_desc"

    by_status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM documents GROUP BY status")}
    counts = {key: sum(by_status.get(st, 0) for st in stored) for key, stored in ingest.STATUS_FILTERS.items()}
    total = sum(by_status.values())

    if "d_status" in request.args:
        f_status = ingest.STATUS_FILTER_ALIASES.get(request.args.get("d_status") or "", request.args.get("d_status") or "")
    else:
        # arriving scoped to a property (e.g. from a "missing source" upload) shows all of its documents
        f_status = "review" if counts["review"] and not prefill_property else ""
    if f_status not in ingest.STATUS_FILTERS:
        f_status = ""
    f_q = (request.args.get("d_q") or "").strip()

    clauses, params = ["1=1"], []
    if f_property:
        clauses.append("d.property_id=?"); params.append(f_property)
    if f_type:
        clauses.append("d.doc_type=?"); params.append(f_type)
    if f_status:
        stored = ingest.STATUS_FILTERS[f_status]
        clauses.append(f"d.status IN ({','.join('?' * len(stored))})"); params += list(stored)
    if f_period and len(f_period) == 7 and f_period[:4].isdigit() and f_period[5:].isdigit():
        clauses.append("d.detected_year=? AND d.detected_month=?"); params += [int(f_period[:4]), int(f_period[5:])]
    if f_q:
        clauses.append("d.filename LIKE ?"); params.append(f"%{f_q}%")

    docs = conn.execute(
        f"SELECT d.* FROM documents d WHERE {' AND '.join(clauses)} ORDER BY {_DOC_SORTS[sort]} LIMIT 200", params
    ).fetchall()
    property_names = {p["id"]: p["name"] for p in get_properties(conn)}
    rows = []
    for d in docs:
        item_stats = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM document_items WHERE document_id=? AND include=1",
            (d["id"],),
        ).fetchone()
        label, kind = ingest.status_display(d["status"])
        rows.append({**dict(d), "property_name": property_names.get(d["property_id"], "Unassigned"),
                     "doc_type_label": DOC_TYPE_LABELS.get(d["doc_type"], d["doc_type"] or "—"),
                     "item_count": item_stats["n"], "item_amount": item_stats["amt"],
                     "period": _period_text(d) or "—", "status_label": label, "status_kind": kind,
                     "possible_duplicate": bool(d["duplicate_of_document"])})

    periods = [{"value": f"{r['detected_year']}-{r['detected_month']:02d}", "label": f"{MONTH_NAMES[r['detected_month']][:3]} {r['detected_year']}"}
               for r in conn.execute("SELECT DISTINCT detected_year, detected_month FROM documents WHERE detected_year IS NOT NULL AND detected_month IS NOT NULL ORDER BY 1 DESC, 2 DESC")]

    def sort_href(column):
        """Clicking a header sorts by it; clicking the active one flips direction."""
        cur_col, cur_dir = sort.rsplit("_", 1)
        direction = ("asc" if cur_dir == "desc" else "desc") if cur_col == column else "desc"
        args = {"d_status": f_status, "d_property": f_property or None, "d_type": f_type or None,
                "d_period": f_period or None, "d_q": f_q or None, "d_sort": f"{column}_{direction}"}
        return url_for("documents.index", **{k: v for k, v in args.items() if v is not None})

    return render_template(
        "documents.html", active="documents", all_properties=get_properties(conn), active_property=None, recon_needed=rc.needed_count(conn),
        docs=rows, counts=counts, total=total, doc_types=DOC_TYPE_LABELS, status_labels=ingest.STATUS_FILTER_LABELS,
        periods=periods, sort=sort, sort_href=sort_href, extraction_available=extraction_available(),
        prefill_property=prefill_property, prefill_type=prefill_type,
        prefill_property_name=property_names.get(prefill_property) if prefill_property else None,
        f_property=f_property, f_type=f_type, f_status=f_status, f_period=f_period, f_q=f_q,
    )


def extraction_available():
    import services.extraction as extraction
    return extraction.available()


@bp.route("/documents/upload", methods=["POST"])
def upload():
    conn = db.get_conn()
    files = request.files.getlist("document")
    files = [f for f in files if f and f.filename]
    if not files:
        flash("Choose at least one file before uploading.", "warning")
        return redirect(url_for("documents.index"))
    doc_type = request.form.get("doc_type", "other")
    property_id = request.form.get("property_id") or None
    last_doc_id = None
    for file in files:
        last_doc_id = save_upload(conn, file, doc_type, property_id, flash)
    if len(files) == 1 and last_doc_id:
        return redirect(url_for("documents.review", doc_id=last_doc_id))
    return redirect(url_for("documents.index"))


@bp.route("/property/<property_id>/upload", methods=["POST"])
def upload_property(property_id):
    conn = db.get_conn()
    file = request.files.get("document")
    if not file or not file.filename:
        flash("Choose a file before uploading.", "warning")
        return redirect(url_for("properties.detail", property_id=property_id))
    doc_id = save_upload(conn, file, request.form.get("doc_type", "other"), property_id, flash)
    if not doc_id:
        return redirect(url_for("properties.documents_tab", property_id=property_id))
    return redirect(url_for("documents.review", doc_id=doc_id))


def _live_duplicate(conn, row):
    """What a transaction line duplicates in the ledger *right now* -- the
    stored duplicate_of is only a snapshot from extraction time and goes
    stale if a twin document is confirmed in between."""
    return find_duplicate(conn, row["property_id"], {"amount": row["amount"], "date": row["date"],
                                                     "vendor": row["vendor"], "description": row["raw_description"]})


def _refresh_duplicates(conn, doc_id):
    """Persist current duplicate flags on an unconfirmed document's lines
    (so uploading the same file twice, then confirming the first, flags the second)."""
    for it in conn.execute("SELECT * FROM document_items WHERE document_id=? AND item_kind='transaction'", (doc_id,)).fetchall():
        live = _live_duplicate(conn, it)
        if live != it["duplicate_of"]:
            conn.execute("UPDATE document_items SET duplicate_of=? WHERE id=?", (live, it["id"]))


def _size_text(n):
    if not n:
        return None
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(n / 1024, 1):.0f} KB"


def _summary_strip(doc, prop, items, detection, names):
    """Everything a reviewer needs to judge the parse before confirming:
    what the file was taken to be, which property/period was chosen and
    how, what was read, and how sure the parser was."""
    res = bool(items and items[0]["item_kind"] == "reservation")
    amount_of = (lambda i: i["net_revenue"]) if res else (lambda i: i["amount"])
    vals = [(i, abs(amount_of(i) or 0)) for i in items]
    extracted_total = detection.get("total")
    if extracted_total is None:
        extracted_total = round(sum(v for _, v in vals), 2)
    included_total = round(sum(v for i, v in vals if i["include"]), 2)
    income = round(sum(v for i, v in vals if not res and i["direction"] == "income"), 2)
    expense = round(sum(v for i, v in vals if not res and i["direction"] != "income"), 2)
    prop_info = detection.get("property") or {}
    confs = [i["confidence"] for i in items if i["confidence"] is not None]
    low = sum(1 for c in confs if c < 0.7)
    if prop:
        prop_how = prop_info.get("source") or "selected"
    elif prop_info.get("candidates"):
        prop_how = "unsure: " + " or ".join(c["name"] for c in prop_info["candidates"][:2])
    else:
        prop_how = "not detected"
    multi_props = len({i["property_id"] for i in items if i["property_id"]})
    return {
        "filename": doc["filename"], "size": _size_text(doc["file_size"]), "uploaded": doc["uploaded_at"],
        "doc_type": DOC_TYPE_LABELS.get(doc["doc_type"], doc["doc_type"] or "Document"),
        "property": prop["name"] if prop else ("Multiple properties (%d)" % multi_props if multi_props > 1 else None),
        "property_how": prop_how if not (multi_props > 1 and not prop) else "matched line by line",
        "period": _period_text(doc), "period_how": (detection.get("period") or {}).get("source"),
        "rows": len(items), "included": sum(1 for i in items if i["include"]), "noun": "reservation" if res else "line",
        "extracted_total": extracted_total, "included_total": included_total, "income": income, "expense": expense,
        "edited_total": abs(included_total - extracted_total) > 0.004 and doc["status"] != "confirmed",
        "confidence": round(sum(confs) / len(confs) * 100) if confs else None, "low_confidence": low,
        "method": detection.get("method"),
    }


@bp.route("/documents/<int:doc_id>/review")
def review(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("We couldn't find that document. It may have been removed; check the Documents list.", "error")
        return redirect(url_for("overview.index"))
    prop = get_property(conn, doc["property_id"])
    items = conn.execute(
        "SELECT *, raw_description AS description FROM document_items WHERE document_id=? ORDER BY line_index", (doc_id,)
    ).fetchall()
    today = datetime.date.today()
    year = doc["detected_year"] or today.year
    month = doc["detected_month"] or today.month
    res_mode = bool(items and items[0]["item_kind"] == "reservation") or (not items and doc["doc_type"] == "booking_statement")
    confirmed = doc["status"] == "confirmed"

    views = []
    for it in items:
        orig = review_helpers.original_of(it)
        row = it
        if not confirmed and it["item_kind"] == "transaction":
            row = {**dict(it), "duplicate_of": _live_duplicate(conn, it)}
        dup = None if confirmed else review_helpers.duplicate_info(conn, row, find_duplicate_reservation)
        include = bool(it["include"])
        if dup:  # a duplicate replaces the earlier upload (or is left out if it matches the Excel history) unless you choose otherwise
            include = (it["dup_decision"] or dup["default"]) != "exclude"
        overlap = ingest.excel_overlap(conn, it["property_id"], it["check_in"]) if (not confirmed and not dup and it["item_kind"] == "reservation") else 0.0
        views.append({
            "row": row, "dup": dup, "include": include, "changed": review_helpers.changed_fields(it), "excel_overlap": overlap,
            "orig": {"note": orig.get("_note"), "status": orig.get("status"), "status_note": orig.get("_status_note"), "po": orig.get("po"), "po_note": orig.get("_po_note"), "order_id": orig.get("order_id"),
                     "vendor": orig.get("vendor"), "description": orig.get("description"), "amount": orig.get("amount"),
                     "category": orig.get("category"), "date": orig.get("date"),
                     "check_in": orig.get("check_in"), "check_out": orig.get("check_out"), "net": orig.get("net"),
                     "gross": orig.get("gross"), "fees": orig.get("fees")},
        })

    summary = None
    created = {"transactions": [], "bookings": []}
    if confirmed:
        created["transactions"] = conn.execute(
            """SELECT t.id, t.date, t.vendor, t.description, t.amount, t.category, p.name AS property_name
               FROM transactions t JOIN properties p ON p.id=t.property_id WHERE t.document_id=? ORDER BY t.date, t.id""", (doc_id,)).fetchall()
        created["bookings"] = conn.execute(
            """SELECT b.id, b.check_in, b.check_out, b.platform, b.net_revenue, p.name AS property_name
               FROM bookings b JOIN properties p ON p.id=b.property_id WHERE b.document_id=? ORDER BY b.check_in, b.id""", (doc_id,)).fetchall()
        added = len(created["transactions"]) + len(created["bookings"])
        month_src = [(v["row"]["check_in"] if v["row"]["item_kind"] == "reservation" else v["row"]["date"]) for v in views if v["row"]["include"]]
        month_src = sorted(m[:7] for m in month_src if m and len(m) >= 7)
        summary = {"month": month_src[0] if month_src else None, "added": added, "excluded": sum(1 for v in views if not v["row"]["include"]),
                   "corrected": sum(1 for v in views if v["changed"]),
                   "noun": "reservation" if res_mode else "transaction"}

    focus_id = request.args.get("item", type=int)
    focus = next((v["row"] for v in views if v["row"]["id"] == focus_id), None) if focus_id else None
    fname = (doc["filename"] or "").lower()
    is_pdf = fname.endswith(".pdf")
    is_image = fname.endswith((".png", ".jpg", ".jpeg", ".webp"))
    preview = review_helpers.file_preview(doc["stored_path"]) if not (is_pdf or is_image) else None

    detection = ingest.detection_of(doc)
    names = {p["id"]: p["name"] for p in get_properties(conn)}
    # reservation lines whose listing matches none of your properties yet: one decision per listing, remembered for next time
    unmatched = []
    if res_mode and not confirmed:
        unmatched = [{"listing": r["raw_description"], "n": r["n"]} for r in conn.execute(
            """SELECT raw_description, COUNT(*) n FROM document_items WHERE document_id=? AND item_kind='reservation'
                 AND property_id IS NULL AND raw_description IS NOT NULL AND raw_description != ''
               GROUP BY raw_description ORDER BY n DESC, raw_description""", (doc_id,))]
    overlap_lines = [v for v in views if v["excel_overlap"]]
    overlap = {"lines": len(overlap_lines), "ticked": sum(1 for v in overlap_lines if v["include"])}
    events = ingest.events_for(conn, doc_id)
    kpi_changes = next((e["detail"].get("kpi_changes") for e in reversed(events)
                        if e["event"] == "confirmed" and e["detail"]), None)
    dup_doc = conn.execute("SELECT id, filename, status, uploaded_at FROM documents WHERE id=?", (doc["duplicate_of_document"],)).fetchone() \
        if doc["duplicate_of_document"] else None
    return render_template(
        "review_document.html", active="documents", all_properties=get_properties(conn),
        active_property=doc["property_id"], prop=prop, doc=doc, items=views,
        flats=get_properties(conn, include_overhead=not res_mode), year=year, month=month, unmatched=unmatched,
        categories=CATEGORIES, is_pdf=is_pdf, is_image=is_image, res_mode=res_mode, confirmed=confirmed,
        summary=summary, preview=preview, focus=focus, doc_type_label=DOC_TYPE_LABELS.get(doc["doc_type"], doc["doc_type"] or "Document"),
        period_label=_period_text(doc), status_label=ingest.status_display(doc["status"])[0],
        strip=_summary_strip(doc, prop, items, detection, names), warnings=detection.get("warnings", []),
        events=events, kpi_changes=kpi_changes, created=created, dup_doc=dup_doc, overlap=overlap,
        extraction_available=extraction_available(),
        needs_ai=not fname.endswith((".csv", ".tsv", ".xls", ".xlsx", ".xlsm")),
    )


def _valid_date(text):
    try:
        datetime.date.fromisoformat(text or "")
        return True
    except ValueError:
        return False


def _num(text):
    try:
        return abs(float(text))
    except (TypeError, ValueError):
        return None


def _problem_message(problems, noun):
    parts = []
    if problems["duplicate"]:
        parts.append(f"{problems['duplicate']} possible duplicate{'s' if problems['duplicate'] != 1 else ''} to decide on")
    if problems["property"]:
        parts.append(f"{problems['property']} line{'s' if problems['property'] != 1 else ''} without a property")
    if problems["amount"]:
        parts.append(f"{problems['amount']} without an amount")
    if problems["date"]:
        parts.append(f"{problems['date']} without a valid date")
    return f"Nothing was added yet. Still needed before confirming: {', '.join(parts)}. Your edits are saved."


def _names(conn):
    return {p["id"]: p["name"] for p in get_properties(conn)}


def _months_between(check_in, check_out):
    """Every (year, month) a stay touches (check-out day itself excluded)."""
    try:
        a, b = datetime.date.fromisoformat(check_in), datetime.date.fromisoformat(check_out) - datetime.timedelta(days=1)
    except ValueError:
        return []
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _log_edits(conn, doc_id):
    """Record what the reviewer changed relative to what the parser read --
    once per distinct set of edits, not on every blocked confirm attempt."""
    detail = []
    for it in conn.execute("SELECT * FROM document_items WHERE document_id=? ORDER BY line_index", (doc_id,)):
        changes = review_helpers.field_changes(it)
        if changes:
            detail.append({"line": it["vendor"] or it["raw_description"] or f"line {it['line_index'] + 1}", "changes": changes})
    if not detail:
        return
    last = conn.execute("SELECT detail FROM document_events WHERE document_id=? AND event='edited' ORDER BY id DESC LIMIT 1", (doc_id,)).fetchone()
    if last and json.loads(last["detail"] or "null") == json.loads(json.dumps(detail)):
        return
    n = sum(len(d["changes"]) for d in detail)
    ingest.log_event(conn, doc_id, "edited", f"{n} value{'s' if n != 1 else ''} changed on {len(detail)} line{'s' if len(detail) != 1 else ''}", detail)


def _log_confirmed(conn, doc_id, noun, added, excluded, corrected, before, after, manual=False, replaced=0):
    changes = ingest.kpi_changes(before, after, _names(conn))
    parts = [f"{replaced} replaced an earlier version" if replaced else "", f"{excluded} excluded" if excluded else "", f"{corrected} corrected" if corrected else "", "entered by hand" if manual else ""]
    extra = ", ".join(p for p in parts if p)
    ingest.log_event(conn, doc_id, "confirmed", f"{added} {noun}{'s' if added != 1 else ''} created" + (f" ({extra})" if extra else ""),
                     {"added": added, "replaced": replaced, "noun": noun, "excluded": excluded, "corrected": corrected, "manual": manual, "kpi_changes": changes})


def _finish(conn, doc_id, final_property_id, added, noun, excluded, corrected, replaced=0):
    conn.execute("UPDATE documents SET status='confirmed', reviewed=1, property_id=? WHERE id=?", (final_property_id, doc_id))
    conn.commit()
    extra = " · ".join(x for x in (f"{replaced} replaced an earlier version" if replaced else "", f"{excluded} excluded" if excluded else "", f"{corrected} manually corrected" if corrected else "") if x)
    flash(f"\u2713 {added} {noun}{'s' if added != 1 else ''} added" + (f" ({extra})" if extra else ""), "success")


@bp.route("/documents/<int:doc_id>/confirm", methods=["POST"])
def confirm(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("We couldn't find that document. It may have been removed; check the Documents list.", "error")
        return redirect(url_for("overview.index"))
    if doc["status"] == "confirmed":
        flash("This document was already confirmed, so its lines are already in your records.", "info")
        return redirect(url_for("documents.review", doc_id=doc_id))

    _refresh_duplicates(conn, doc_id)
    has_items = conn.execute("SELECT 1 FROM document_items WHERE document_id=? LIMIT 1", (doc_id,)).fetchone()
    included = set(request.form.getlist("include"))

    first_kind = conn.execute("SELECT item_kind FROM document_items WHERE document_id=? LIMIT 1", (doc_id,)).fetchone()
    if (first_kind and first_kind["item_kind"] == "reservation") or (not has_items and doc["doc_type"] == "booking_statement"):
        return _confirm_reservations(conn, doc, doc_id, bool(has_items), included)
    if has_items:
        return _confirm_transactions(conn, doc, doc_id, included)

    # Nothing was auto-extracted -- the manual blank-row fallback, not backed by document_items.
    f = request.form
    entries, left_out_excel = [], []
    for i, (pid, vendor, desc, amount_s, category, year_s, month_s) in enumerate(zip(
            f.getlist("property_id"), f.getlist("vendor"), f.getlist("description"), f.getlist("amount"),
            f.getlist("category"), f.getlist("year"), f.getlist("month"))):
        amount = _num(amount_s)
        if str(i) not in included or not amount or not pid:
            continue
        date = f"{int(year_s)}-{int(month_s):02d}-01"
        dup_id = None if f.get("allow_dups") else find_duplicate(conn, pid, {"amount": amount, "date": date, "vendor": vendor, "description": desc})
        dup_src = ingest.source_of(conn, "transactions", dup_id)
        if dup_src in ingest.PROTECTED_SOURCES:      # already in the Excel history: kept untouched, not added again
            left_out_excel.append(f"{vendor or desc or 'a line'} £{amount:,.2f}")
            continue
        entries.append((pid, vendor, desc, amount, category, date, dup_id))   # dup_id set = replaces that earlier upload
    if left_out_excel:
        flash(f"{', '.join(left_out_excel)} {'is' if len(left_out_excel) == 1 else 'are'} already in your Excel history, which is kept untouched, so {'it was' if len(left_out_excel) == 1 else 'they were'} not added again. "
              "Tick \u201cAdd even if they look like duplicates\u201d to add anyway.", "info")
    touched = {(e[0], *ingest.month_key(e[5])) for e in entries}
    before = ingest.kpi_snapshot(conn, touched)
    final_property_id = doc["property_id"]
    replaced = 0
    for pid, vendor, desc, amount, category, date, dup_id in entries:
        direction = "income" if category == "booking_income" else "expense"
        if dup_id:
            old = conn.execute("SELECT * FROM transactions WHERE id=?", (dup_id,)).fetchone()
            record_edits(conn, "transaction", dup_id, old, {"property_id": pid, "date": date, "vendor": vendor, "description": desc, "amount": amount, "category": category})
            record(conn, "transaction", dup_id, "replace", field="document", old_value=old["document_id"], new_value=doc_id)
            conn.execute("""UPDATE transactions SET property_id=?, date=?, vendor=?, vendor_id=?, description=?, amount=?, direction=?, category=?,
                              source='upload', document_id=?, edited_at=datetime('now') WHERE id=?""",
                         (pid, date, vendor, get_or_create_vendor(conn, vendor), desc, amount, direction, category, doc_id, dup_id))
            replaced += 1
        else:
            conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, vendor_id, description, amount, direction, category, source, document_id)
                   VALUES (?,?,?,?,?,?,?,?,'upload',?)""",
                (pid, date, vendor, get_or_create_vendor(conn, vendor), desc, amount, direction, category, doc_id))
        final_property_id = pid
    _log_confirmed(conn, doc_id, "transaction", len(entries) - replaced, 0, 0, before, ingest.kpi_snapshot(conn, touched), manual=True, replaced=replaced)
    _finish(conn, doc_id, final_property_id, len(entries) - replaced, "transaction", 0, 0, replaced)
    return redirect(url_for("documents.review", doc_id=doc_id))


def _confirm_transactions(conn, doc, doc_id, included):
    f = request.form
    ids = f.getlist("item_id")
    cols = {k: f.getlist(k) for k in ("property_id", "vendor", "description", "amount", "category", "type", "date")}
    problems = {"duplicate": 0, "property": 0, "amount": 0, "date": 0}
    ready, excluded, corrected = [], 0, 0
    for idx, item_id in enumerate(ids):
        item = conn.execute("SELECT * FROM document_items WHERE id=? AND document_id=?", (item_id, doc_id)).fetchone()
        if not item:
            continue
        pid, vendor, desc = cols["property_id"][idx], cols["vendor"][idx], cols["description"][idx]
        category, date = cols["category"][idx], cols["date"][idx]
        amount = _num(cols["amount"][idx])
        capex = 1 if cols["type"][idx] == "capex" else 0
        dup_src = ingest.source_of(conn, "transactions", item["duplicate_of"])
        is_dup = dup_src is not None
        decision = f.get(f"dup_{item_id}") or (ingest.duplicate_default(dup_src) if is_dup else None)
        if is_dup and dup_src in ingest.PROTECTED_SOURCES and decision == "replace":
            decision = "exclude"          # the Excel history is never overwritten
        include_row = item_id in included and not (is_dup and decision == "exclude")
        replace_id = item["duplicate_of"] if (include_row and is_dup and decision == "replace") else None
        if include_row:
            if not pid:
                problems["property"] += 1
            if not amount:
                problems["amount"] += 1
            if not _valid_date(date):
                problems["date"] += 1
        else:
            excluded += 1
        direction = "income" if category == "booking_income" else "expense"
        conn.execute(
            """UPDATE document_items SET property_id=?, vendor=?, raw_description=?, amount=?, category=?, capex=?, date=?,
                 direction=?, include=?, dup_decision=?, reviewed=1, final_value=? WHERE id=?""",
            (pid or None, vendor, desc, amount, category, capex, date, direction, 1 if include_row else 0, decision,
             json.dumps({"property_id": pid, "vendor": vendor, "description": desc, "amount": amount, "category": category,
                         "capex": capex, "date": date, "include": include_row}), item_id))
        fresh = conn.execute("SELECT * FROM document_items WHERE id=?", (item_id,)).fetchone()
        corrected += 1 if review_helpers.changed_fields(fresh) else 0
        if include_row:
            ready.append((item_id, pid, vendor, desc, amount, category, capex, date, direction, replace_id))

    _log_edits(conn, doc_id)
    if any(problems.values()):
        conn.execute("UPDATE document_items SET reviewed=0 WHERE document_id=?", (doc_id,))
        conn.commit()
        flash(_problem_message(problems, "transaction"), "warning")
        return redirect(url_for("documents.review", doc_id=doc_id))

    final_property_id = doc["property_id"]
    touched = {(r[1], *ingest.month_key(r[7])) for r in ready}
    before = ingest.kpi_snapshot(conn, touched)
    replaced = 0
    for item_id, pid, vendor, desc, amount, category, capex, date, direction, replace_id in ready:
        if replace_id:   # the same line from an earlier upload: the new one takes its place (old values kept in the audit log)
            old = conn.execute("SELECT * FROM transactions WHERE id=?", (replace_id,)).fetchone()
            new_values = {"property_id": pid, "date": date, "vendor": vendor, "description": desc, "amount": amount, "category": category, "capex": capex}
            record_edits(conn, "transaction", replace_id, old, new_values)
            record(conn, "transaction", replace_id, "replace", field="document", old_value=old["document_id"], new_value=doc_id)
            conn.execute(
                """UPDATE transactions SET property_id=?, date=?, vendor=?, vendor_id=?, description=?, amount=?, direction=?, category=?, capex=?,
                     source='upload', document_id=?, edited_at=datetime('now') WHERE id=?""",
                (pid, date, vendor, get_or_create_vendor(conn, vendor), desc, amount, direction, category, capex, doc_id, replace_id))
            conn.execute("UPDATE document_items SET duplicate_of=? WHERE id=?", (replace_id, item_id))
            replaced += 1
        else:
            cur = conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, vendor_id, description, amount, direction, category, capex, source, document_id)
                   VALUES (?,?,?,?,?,?,?,?,?,'upload',?)""",
                (pid, date, vendor, get_or_create_vendor(conn, vendor), desc, amount, direction, category, capex, doc_id))
            conn.execute("UPDATE document_items SET duplicate_of=? WHERE id=?", (cur.lastrowid, item_id))
        final_property_id = pid
    _log_confirmed(conn, doc_id, "transaction", len(ready) - replaced, excluded, corrected, before, ingest.kpi_snapshot(conn, touched), replaced=replaced)
    _finish(conn, doc_id, final_property_id, len(ready) - replaced, "transaction", excluded, corrected, replaced)
    return redirect(url_for("documents.review", doc_id=doc_id))


def _confirm_reservations(conn, doc, doc_id, has_items, included):
    """Booking-statement lines become real `bookings` rows (source='upload'),
    never income transactions -- kpis.py sums both, so writing a reservation
    as both would count it twice."""
    f = request.form
    item_ids = f.getlist("item_id")
    n = len(f.getlist("check_in"))
    problems = {"duplicate": 0, "property": 0, "amount": 0, "date": 0}
    ready, excluded, corrected = [], 0, 0
    for i in range(n):
        row_key = item_ids[i] if has_items else str(i)
        pid = f.getlist("property_id")[i]
        check_in, check_out = f.getlist("check_in")[i], f.getlist("check_out")[i]
        net, gross, fees = _num(f.getlist("net")[i]), _num(f.getlist("gross")[i]), _num(f.getlist("fees")[i]) or 0
        if net is None and gross is not None:
            net = max(gross - fees, 0)
        platform = f.getlist("platform")[i].strip() or None
        code = f.getlist("reservation_id")[i].strip() or None
        decision = f.get(f"dup_{row_key}") or None
        include_row = row_key in included
        dup_id = None
        if pid and (has_items or include_row):   # judged on the property chosen now, not the one read at upload
            dup_id = find_duplicate_reservation(conn, pid, {"reservation_id": code, "check_in": check_in, "check_out": check_out})
        dup_src = ingest.source_of(conn, "bookings", dup_id)
        is_dup = dup_src is not None
        if is_dup:
            if not has_items and f.get("allow_dups"):
                decision = "keep"
            decision = decision or ingest.duplicate_default(dup_src)
            if dup_src in ingest.PROTECTED_SOURCES and decision == "replace":
                decision = "exclude"      # the Excel history is never overwritten
            if decision == "exclude":
                include_row = False
        replace_id = dup_id if (include_row and is_dup and decision == "replace") else None
        if include_row:
            if not pid:
                problems["property"] += 1
            if net is None:
                problems["amount"] += 1
            if not (_valid_date(check_in) and _valid_date(check_out) and check_out > check_in):
                problems["date"] += 1
        else:
            excluded += 1
        if has_items:
            conn.execute(
                """UPDATE document_items SET property_id=?, platform=?, reservation_id=?, check_in=?, check_out=?,
                     gross_revenue=?, platform_fees=?, net_revenue=?, amount=?, include=?, dup_decision=?, reviewed=1 WHERE id=?""",
                (pid or None, platform, code, check_in, check_out, gross, fees, net, net, 1 if include_row else 0, decision, row_key))
            corrected += 1 if review_helpers.changed_fields(conn.execute("SELECT * FROM document_items WHERE id=?", (row_key,)).fetchone()) else 0
        if include_row:
            ready.append((pid, platform, code, check_in, check_out, gross, fees, net, replace_id))

    if has_items:
        _log_edits(conn, doc_id)
    if any(problems.values()):
        if has_items:
            conn.execute("UPDATE document_items SET reviewed=0 WHERE document_id=?", (doc_id,))
        conn.commit()
        flash(_problem_message(problems, "reservation") + ("" if has_items else " To add a hand-entered reservation that looks like one already on file, tick \u201cAdd even if they look like duplicates\u201d."), "warning")
        return redirect(url_for("documents.review", doc_id=doc_id))

    kpi_keys = {(r[0], *ym) for r in ready for ym in _months_between(r[3], r[4])}
    kpi_before = ingest.kpi_snapshot(conn, kpi_keys)
    touched, final_property_id, replaced = {}, doc["property_id"], 0
    for pid, platform, code, check_in, check_out, gross, fees, net, replace_id in ready:
        key = (pid, check_in[:7])
        if key not in touched:
            y, m = map(int, key[1].split("-"))
            touched[key] = kpis.revenue(conn, pid, *kpis.month_bounds(y, m))
        gross_v = gross if gross is not None else net
        if replace_id:   # the same reservation from an earlier upload: this version takes its place (old values kept in the audit log)
            old = conn.execute("SELECT * FROM bookings WHERE id=?", (replace_id,)).fetchone()
            record_edits(conn, "booking", replace_id, old, {"property_id": pid, "platform": platform, "reservation_id": code, "check_in": check_in,
                                                          "check_out": check_out, "gross_revenue": gross_v, "platform_fees": fees, "net_revenue": net})
            record(conn, "booking", replace_id, "replace", field="document", old_value=old["document_id"], new_value=doc_id)
            conn.execute(
                """UPDATE bookings SET property_id=?, platform=?, reservation_id=?, check_in=?, check_out=?, gross_revenue=?, platform_fees=?,
                     net_revenue=?, status='confirmed', source='upload', document_id=? WHERE id=?""",
                (pid, platform, code, check_in, check_out, gross_v, fees, net, doc_id, replace_id))
            replaced += 1
        else:
            conn.execute(
                """INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue,
                       platform_fees, cleaning_fee, net_revenue, status, source, document_id)
                   VALUES (?,?,?,?,?,?,?,0,?,'confirmed','upload',?)""",
                (pid, platform, code, check_in, check_out, gross_v, fees, net, doc_id))
        final_property_id = pid
    if has_items:  # next statement: these listings are matched without being asked again
        for it in conn.execute("SELECT raw_description, property_id FROM document_items WHERE document_id=? AND item_kind='reservation' AND include=1 AND property_id IS NOT NULL", (doc_id,)):
            ingest.alias_remember(conn, it["raw_description"], it["property_id"])
    _log_confirmed(conn, doc_id, "reservation", len(ready) - replaced, excluded, corrected, kpi_before, ingest.kpi_snapshot(conn, kpi_keys), manual=not has_items, replaced=replaced)
    _finish(conn, doc_id, final_property_id, len(ready) - replaced, "reservation", excluded, corrected, replaced)
    names = {p["id"]: p["name"] for p in get_properties(conn)}
    for (pid, ym), before in touched.items():
        y, m = map(int, ym.split("-"))
        after = kpis.revenue(conn, pid, *kpis.month_bounds(y, m))
        if abs(after - before) < 0.005:
            flash(f"{names.get(pid, pid)}, {MONTH_NAMES[m]} {y}: stored. Dashboard figures are unchanged (the Excel history is still in charge of this month). "
                  f"Compare the two on the Reconciliation page.", "info")
        else:
            flash(f"{names.get(pid, pid)}, {MONTH_NAMES[m]} {y}: £{before:,.0f} → £{after:,.0f} (no Excel history for this month, so these reservations now feed the dashboard).", "info")
    return redirect(url_for("documents.review", doc_id=doc_id))


@bp.route("/documents/<int:doc_id>/undo", methods=["POST"])
def undo(doc_id):
    """Reverses a confirmed import: removes the transactions/reservations it
    created and puts the document back to 'needs review' with its extracted
    lines intact, so it can be corrected and confirmed again."""
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc or doc["status"] != "confirmed":
        flash("Only a confirmed document can be undone. This one hasn't been confirmed yet.", "info")
        return redirect(url_for("documents.index"))
    # confirm() points document_items.duplicate_of at the transaction each line became
    conn.execute("UPDATE document_items SET duplicate_of=NULL WHERE document_id=? AND duplicate_of IN (SELECT id FROM transactions WHERE document_id=?)", (doc_id, doc_id))
    conn.execute("UPDATE document_items SET reviewed=0 WHERE document_id=?", (doc_id,))
    n_tx = conn.execute("DELETE FROM transactions WHERE document_id=? AND source='upload'", (doc_id,)).rowcount
    n_bk = conn.execute("DELETE FROM bookings WHERE document_id=? AND source='upload'", (doc_id,)).rowcount
    conn.execute("UPDATE documents SET status='extracted', reviewed=0 WHERE id=?", (doc_id,))
    record(conn, "document", doc_id, "delete", field="import", old_value=f"{n_tx} transactions, {n_bk} reservations")
    ingest.log_event(conn, doc_id, "undone", f"Import undone: removed {n_tx} transaction{'s' if n_tx != 1 else ''} and {n_bk} reservation{'s' if n_bk != 1 else ''}",
                     {"transactions": n_tx, "reservations": n_bk})
    conn.commit()
    flash(f"\u2713 Import undone: removed {n_tx} transaction{'s' if n_tx != 1 else ''} and {n_bk} reservation{'s' if n_bk != 1 else ''}. "
          f"Your Excel history was never touched.", "success")
    return redirect(url_for("documents.review", doc_id=doc_id))


@bp.route("/documents/<int:doc_id>/reject", methods=["POST"])
def reject(doc_id):
    """Throw away an unconfirmed draft: its extracted lines and the stored
    file. Nothing was ever in the ledger, so no figure moves. A confirmed
    import must be undone first."""
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("That document is already gone.", "info")
        return redirect(url_for("documents.index"))
    if doc["status"] == "confirmed":
        flash("This import is confirmed and its records are in your figures. Undo the import first, then you can delete the draft.", "warning")
        return redirect(url_for("documents.review", doc_id=doc_id))
    n_items = conn.execute("DELETE FROM document_items WHERE document_id=?", (doc_id,)).rowcount
    conn.execute("DELETE FROM document_events WHERE document_id=?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    record(conn, "document", doc_id, "delete", field="draft", old_value=f"{doc['filename']} ({n_items} extracted lines)")
    conn.commit()
    try:
        stored = Path(doc["stored_path"]).resolve()
        if stored.is_file() and runtime.uploads_dir().resolve() in stored.parents:
            stored.unlink()
    except OSError:
        pass  # the draft is gone from the app either way; a stray file on disk is harmless
    flash(f"\u2713 Deleted the draft \u201c{doc['filename']}\u201d. Nothing was added to your figures.", "success")
    return redirect(url_for("documents.index"))


@bp.route("/documents/<int:doc_id>/map-listing", methods=["POST"])
def map_listing(doc_id):
    """One decision for every reservation line carrying the same listing name:
    which of your properties it is, or that it isn't one of yours (left out).
    Remembered, so the next statement is matched automatically."""
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    listing = (request.form.get("listing") or "").strip()
    choice = request.form.get("property_id") or ""
    if not doc or doc["status"] == "confirmed" or not listing or not choice:
        flash("Choose a property for that listing first.", "warning")
        return redirect(url_for("documents.review", doc_id=doc_id))
    if choice == "__ignore__":
        n = conn.execute("UPDATE document_items SET include=0 WHERE document_id=? AND item_kind='reservation' AND raw_description=? AND property_id IS NULL",
                         (doc_id, listing)).rowcount
        ingest.alias_remember(conn, listing, None, ignore=True)
        flash(f"\u2713 Left out {n} reservation{'s' if n != 1 else ''} for \u201c{listing}\u201d. It will be skipped on future statements too.", "success")
    else:
        if not get_property(conn, choice):
            flash("That property doesn't exist.", "error")
            return redirect(url_for("documents.review", doc_id=doc_id))
        n = 0
        for it in conn.execute("SELECT id, check_in, check_out, reservation_id FROM document_items WHERE document_id=? AND item_kind='reservation' AND raw_description=? AND property_id IS NULL", (doc_id, listing)).fetchall():
            dup_id = find_duplicate_reservation(conn, choice, {"reservation_id": it["reservation_id"], "check_in": it["check_in"], "check_out": it["check_out"]})
            tick = (ingest.duplicate_default(ingest.source_of(conn, "bookings", dup_id)) == "replace") if dup_id else True
            conn.execute("UPDATE document_items SET property_id=?, include=? WHERE id=?", (choice, 1 if tick else 0, it["id"]))
            n += 1
        ingest.alias_remember(conn, listing, choice)
        flash(f"\u2713 Assigned {n} reservation{'s' if n != 1 else ''} for \u201c{listing}\u201d to {get_property(conn, choice)['name']}. Remembered for next time.", "success")
    ingest.log_event(conn, doc_id, "edited", f"Listing \u201c{listing}\u201d -> {'not tracked' if choice == '__ignore__' else get_property(conn, choice)['name']} ({n} line{'s' if n != 1 else ''})",
                     {"listing": listing, "choice": choice, "lines": n})
    conn.commit()
    return redirect(url_for("documents.review", doc_id=doc_id))
