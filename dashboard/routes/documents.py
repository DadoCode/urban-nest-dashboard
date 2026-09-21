import datetime
import json

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

import db
from services.audit import record
from services.common import CATEGORIES, get_properties, get_property
import services.kpis as kpis
from services.common import MONTH_NAMES
import services.review as review_helpers
from services.documents import find_duplicate_reservation, save_upload
from services.vendors import get_or_create_vendor

bp = Blueprint("documents", __name__)

DOC_TYPE_LABELS = {
    "amazon_order": "Amazon / Temu order", "cleaning_invoice": "Cleaning invoice",
    "booking_statement": "Booking / Airbnb statement", "bank_statement": "Bank statement",
    "utility_bill": "Utility bill", "other": "Other",
}


@bp.route("/documents/<int:doc_id>/drawer")
def drawer(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        return "<p class='note'>Document not found.</p>", 404
    prop = get_property(conn, doc["property_id"])
    stats = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt, SUM(include) inc FROM document_items WHERE document_id=?", (doc_id,)).fetchone()
    return render_template(
        "partials/document_drawer.html", doc=doc, prop=prop, stats=stats,
        type_label=DOC_TYPE_LABELS.get(doc["doc_type"], doc["doc_type"] or "Document"),
        period=f"{MONTH_NAMES[doc['detected_month']]} {doc['detected_year']}" if doc["detected_year"] and doc["detected_month"] else None,
    )


@bp.route("/documents/<int:doc_id>/file")
def file(doc_id):
    doc = db.get_conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        abort(404)
    return send_file(doc["stored_path"], download_name=doc["filename"])


@bp.route("/documents")
def index():
    conn = db.get_conn()
    f_property = request.args.get("d_property") or ""
    f_type = request.args.get("d_type") or ""
    counts = conn.execute(
        """SELECT
             SUM(CASE WHEN status IN ('pending','extracted') THEN 1 ELSE 0 END) needs_review,
             SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) complete,
             SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
             COUNT(*) total
           FROM documents"""
    ).fetchone()
    if "d_status" in request.args:
        f_status = request.args.get("d_status") or ""
    else:
        f_status = "review" if counts["needs_review"] else ""
    f_q = (request.args.get("d_q") or "").strip()

    clauses, params = ["1=1"], []
    if f_property:
        clauses.append("property_id=?"); params.append(f_property)
    if f_type:
        clauses.append("doc_type=?"); params.append(f_type)
    if f_status == "review":
        clauses.append("status IN ('pending','extracted')")
    elif f_status == "complete":
        clauses.append("status='confirmed'")
    elif f_status:
        clauses.append("status=?"); params.append(f_status)
    if f_q:
        clauses.append("filename LIKE ?"); params.append(f"%{f_q}%")

    docs = conn.execute(
        f"SELECT * FROM documents WHERE {' AND '.join(clauses)} ORDER BY uploaded_at DESC LIMIT 200", params
    ).fetchall()
    property_names = {p["id"]: p["name"] for p in get_properties(conn)}
    rows = []
    for d in docs:
        item_stats = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM document_items WHERE document_id=? AND include=1",
            (d["id"],),
        ).fetchone()
        rows.append({**dict(d), "property_name": property_names.get(d["property_id"], "Unassigned"),
                     "doc_type_label": DOC_TYPE_LABELS.get(d["doc_type"], d["doc_type"] or "—"),
                     "item_count": item_stats["n"], "item_amount": item_stats["amt"],
                     "period": f"{MONTH_NAMES[d['detected_month']][:3]} {d['detected_year']}" if d["detected_year"] and d["detected_month"] else "—"})

    return render_template(
        "documents.html", active="documents", all_properties=get_properties(conn), active_property=None,
        docs=rows, counts=counts, doc_types=DOC_TYPE_LABELS,
        f_property=f_property, f_type=f_type, f_status=f_status, f_q=f_q,
    )


@bp.route("/documents/upload", methods=["POST"])
def upload():
    conn = db.get_conn()
    files = request.files.getlist("document")
    files = [f for f in files if f and f.filename]
    if not files:
        flash("Choose at least one file first.")
        return redirect(url_for("documents.index"))
    doc_type = request.form.get("doc_type", "other")
    property_id = request.form.get("property_id") or None
    last_doc_id = None
    for file in files:
        last_doc_id = save_upload(conn, file, doc_type, property_id, flash)
    if len(files) == 1:
        return redirect(url_for("documents.review", doc_id=last_doc_id))
    return redirect(url_for("documents.index"))


@bp.route("/property/<property_id>/upload", methods=["POST"])
def upload_property(property_id):
    conn = db.get_conn()
    file = request.files.get("document")
    if not file or not file.filename:
        flash("Choose a file first.")
        return redirect(url_for("properties.detail", property_id=property_id))
    doc_id = save_upload(conn, file, request.form.get("doc_type", "other"), property_id, flash)
    return redirect(url_for("documents.review", doc_id=doc_id))


@bp.route("/documents/<int:doc_id>/review")
def review(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("Unknown document.")
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
        dup = None if confirmed else review_helpers.duplicate_info(conn, it, find_duplicate_reservation)
        include = bool(it["include"])
        if dup:  # a duplicate is undecided until the reviewer picks; only "exclude" leaves it out
            include = it["dup_decision"] != "exclude"
        views.append({
            "row": it, "dup": dup, "include": include, "changed": review_helpers.changed_fields(it),
            "orig": {"vendor": orig.get("vendor"), "description": orig.get("description"), "amount": orig.get("amount"),
                     "category": orig.get("category"), "date": orig.get("date"),
                     "check_in": orig.get("check_in"), "check_out": orig.get("check_out"), "net": orig.get("net"),
                     "gross": orig.get("gross"), "fees": orig.get("fees")},
        })

    summary = None
    if confirmed:
        added = conn.execute("SELECT COUNT(*) FROM transactions WHERE document_id=?", (doc_id,)).fetchone()[0] \
            + conn.execute("SELECT COUNT(*) FROM bookings WHERE document_id=?", (doc_id,)).fetchone()[0]
        summary = {"added": added, "excluded": sum(1 for v in views if not v["row"]["include"]),
                   "corrected": sum(1 for v in views if v["changed"]),
                   "noun": "reservation" if res_mode else "transaction"}

    focus_id = request.args.get("item", type=int)
    focus = next((v["row"] for v in views if v["row"]["id"] == focus_id), None) if focus_id else None
    fname = (doc["filename"] or "").lower()
    is_pdf = fname.endswith(".pdf")
    is_image = fname.endswith((".png", ".jpg", ".jpeg", ".webp"))
    preview = review_helpers.file_preview(doc["stored_path"]) if not (is_pdf or is_image) else None
    return render_template(
        "review_document.html", active="documents", all_properties=get_properties(conn),
        active_property=doc["property_id"], prop=prop, doc=doc, items=views,
        flats=get_properties(conn, include_overhead=False), year=year, month=month,
        categories=CATEGORIES, is_pdf=is_pdf, is_image=is_image, res_mode=res_mode, confirmed=confirmed,
        summary=summary, preview=preview, focus=focus, doc_type_label=DOC_TYPE_LABELS.get(doc["doc_type"], doc["doc_type"] or "Document"),
        period_label=f"{MONTH_NAMES[doc['detected_month']]} {doc['detected_year']}" if doc["detected_year"] and doc["detected_month"] else None,
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


def _finish(conn, doc_id, final_property_id, added, noun, excluded, corrected):
    conn.execute("UPDATE documents SET status='confirmed', reviewed=1, property_id=? WHERE id=?", (final_property_id, doc_id))
    conn.commit()
    extra = " · ".join(x for x in (f"{excluded} excluded" if excluded else "", f"{corrected} manually corrected" if corrected else "") if x)
    flash(f"\u2713 {added} {noun}{'s' if added != 1 else ''} added" + (f" ({extra})" if extra else ""))


@bp.route("/documents/<int:doc_id>/confirm", methods=["POST"])
def confirm(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("Unknown document.")
        return redirect(url_for("overview.index"))
    if doc["status"] == "confirmed":
        flash("This document was already confirmed -- its transactions are already on the ledger.")
        return redirect(url_for("documents.review", doc_id=doc_id))

    has_items = conn.execute("SELECT 1 FROM document_items WHERE document_id=? LIMIT 1", (doc_id,)).fetchone()
    included = set(request.form.getlist("include"))

    first_kind = conn.execute("SELECT item_kind FROM document_items WHERE document_id=? LIMIT 1", (doc_id,)).fetchone()
    if (first_kind and first_kind["item_kind"] == "reservation") or (not has_items and doc["doc_type"] == "booking_statement"):
        return _confirm_reservations(conn, doc, doc_id, bool(has_items), included)
    if has_items:
        return _confirm_transactions(conn, doc, doc_id, included)

    # Nothing was auto-extracted -- the manual blank-row fallback, not backed by document_items.
    f = request.form
    added, final_property_id = 0, doc["property_id"]
    for i, (pid, vendor, desc, amount_s, category, year_s, month_s) in enumerate(zip(
            f.getlist("property_id"), f.getlist("vendor"), f.getlist("description"), f.getlist("amount"),
            f.getlist("category"), f.getlist("year"), f.getlist("month"))):
        amount = _num(amount_s)
        if str(i) not in included or not amount or not pid:
            continue
        year, month = int(year_s), int(month_s)
        direction = "income" if category == "booking_income" else "expense"
        conn.execute(
            """INSERT INTO transactions (property_id, date, vendor, vendor_id, description, amount, direction, category, source, document_id)
               VALUES (?,?,?,?,?,?,?,?,'upload',?)""",
            (pid, f"{year}-{month:02d}-01", vendor, get_or_create_vendor(conn, vendor), desc, amount, direction, category, doc_id))
        added += 1
        final_property_id = pid
    _finish(conn, doc_id, final_property_id, added, "transaction", 0, 0)
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
        decision = f.get(f"dup_{item_id}") or None
        is_dup = bool(item["duplicate_of"] and conn.execute("SELECT 1 FROM transactions WHERE id=?", (item["duplicate_of"],)).fetchone())
        include_row = item_id in included and not (is_dup and decision == "exclude")
        if include_row:
            if is_dup and decision != "keep":
                problems["duplicate"] += 1
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
            ready.append((item_id, pid, vendor, desc, amount, category, capex, date, direction))

    if any(problems.values()):
        conn.execute("UPDATE document_items SET reviewed=0 WHERE document_id=?", (doc_id,))
        conn.commit()
        flash(_problem_message(problems, "transaction"))
        return redirect(url_for("documents.review", doc_id=doc_id))

    final_property_id = doc["property_id"]
    for item_id, pid, vendor, desc, amount, category, capex, date, direction in ready:
        cur = conn.execute(
            """INSERT INTO transactions (property_id, date, vendor, vendor_id, description, amount, direction, category, capex, source, document_id)
               VALUES (?,?,?,?,?,?,?,?,?,'upload',?)""",
            (pid, date, vendor, get_or_create_vendor(conn, vendor), desc, amount, direction, category, capex, doc_id))
        conn.execute("UPDATE document_items SET duplicate_of=? WHERE id=? AND duplicate_of IS NULL", (cur.lastrowid, item_id))
        final_property_id = pid
    _finish(conn, doc_id, final_property_id, len(ready), "transaction", excluded, corrected)
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
        is_dup = False
        if has_items:
            item = conn.execute("SELECT * FROM document_items WHERE id=?", (row_key,)).fetchone()
            is_dup = bool(find_duplicate_reservation(conn, item["property_id"], {
                "reservation_id": item["reservation_id"], "check_in": item["check_in"], "check_out": item["check_out"]}))
            if is_dup and decision == "exclude":
                include_row = False
        if include_row:
            if is_dup and decision != "keep":
                problems["duplicate"] += 1
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
            ready.append((pid, platform, code, check_in, check_out, gross, fees, net))

    if any(problems.values()):
        if has_items:
            conn.execute("UPDATE document_items SET reviewed=0 WHERE document_id=?", (doc_id,))
        conn.commit()
        flash(_problem_message(problems, "reservation"))
        return redirect(url_for("documents.review", doc_id=doc_id))

    touched, final_property_id = {}, doc["property_id"]
    for pid, platform, code, check_in, check_out, gross, fees, net in ready:
        key = (pid, check_in[:7])
        if key not in touched:
            y, m = map(int, key[1].split("-"))
            touched[key] = kpis.revenue(conn, pid, *kpis.month_bounds(y, m))
        conn.execute(
            """INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue,
                   platform_fees, cleaning_fee, net_revenue, status, source, document_id)
               VALUES (?,?,?,?,?,?,?,0,?,'confirmed','upload',?)""",
            (pid, platform, code, check_in, check_out, gross if gross is not None else net, fees, net, doc_id))
        final_property_id = pid
    _finish(conn, doc_id, final_property_id, len(ready), "reservation", excluded, corrected)
    names = {p["id"]: p["name"] for p in get_properties(conn)}
    for (pid, ym), before in touched.items():
        y, m = map(int, ym.split("-"))
        after = kpis.revenue(conn, pid, *kpis.month_bounds(y, m))
        flash(f"{names.get(pid, pid)}, {MONTH_NAMES[m]} {y} is now based on the reservations on file: "
              f"£{before:,.0f} before, £{after:,.0f} now. If that looks low, the statement may not cover every "
              f"channel or reservation for the month -- upload the rest, or delete these to restore the Excel figure.")
    return redirect(url_for("documents.review", doc_id=doc_id))


@bp.route("/documents/<int:doc_id>/undo", methods=["POST"])
def undo(doc_id):
    """Reverses a confirmed import: removes the transactions/reservations it
    created and puts the document back to 'needs review' with its extracted
    lines intact, so it can be corrected and confirmed again."""
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc or doc["status"] != "confirmed":
        flash("Only a confirmed document can be undone.")
        return redirect(url_for("documents.index"))
    # confirm() points document_items.duplicate_of at the transaction each line became
    conn.execute("UPDATE document_items SET duplicate_of=NULL WHERE document_id=? AND duplicate_of IN (SELECT id FROM transactions WHERE document_id=?)", (doc_id, doc_id))
    conn.execute("UPDATE document_items SET reviewed=0 WHERE document_id=?", (doc_id,))
    n_tx = conn.execute("DELETE FROM transactions WHERE document_id=? AND source='upload'", (doc_id,)).rowcount
    n_bk = conn.execute("DELETE FROM bookings WHERE document_id=? AND source='upload'", (doc_id,)).rowcount
    conn.execute("UPDATE documents SET status='extracted', reviewed=0 WHERE id=?", (doc_id,))
    record(conn, "document", doc_id, "delete", field="import", old_value=f"{n_tx} transactions, {n_bk} reservations")
    conn.commit()
    flash(f"Import undone: removed {n_tx} transaction{'s' if n_tx != 1 else ''} and {n_bk} reservation{'s' if n_bk != 1 else ''}. "
          f"Any month that was based on those reservations goes back to its earlier figures.")
    return redirect(url_for("documents.review", doc_id=doc_id))
