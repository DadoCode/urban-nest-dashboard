import datetime
import json

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

import db
from services.audit import record
from services.common import CATEGORIES, get_properties, get_property
import services.kpis as kpis
from services.common import MONTH_NAMES
from services.documents import find_duplicate_reservation, save_upload
from services.vendors import get_or_create_vendor

bp = Blueprint("documents", __name__)

DOC_TYPE_LABELS = {
    "amazon_order": "Amazon / Temu order", "cleaning_invoice": "Cleaning invoice",
    "booking_statement": "Booking / Airbnb statement", "bank_statement": "Bank statement",
    "utility_bill": "Utility bill", "other": "Other",
}


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
    item_dups = {i["id"] for i in items if i["item_kind"] == "reservation"
                 and find_duplicate_reservation(conn, i["property_id"], {"reservation_id": i["reservation_id"], "check_in": i["check_in"], "check_out": i["check_out"]})}
    is_pdf = (doc["filename"] or "").lower().endswith(".pdf")
    is_image = (doc["filename"] or "").lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    return render_template(
        "review_document.html", active="documents", all_properties=get_properties(conn),
        active_property=doc["property_id"], prop=prop, doc=doc, items=items,
        flats=get_properties(conn, include_overhead=False), year=year, month=month,
        categories=CATEGORIES, is_pdf=is_pdf, is_image=is_image, res_mode=res_mode, item_dups=item_dups,
    )


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
    added = 0
    final_property_id = doc["property_id"]

    first_kind = conn.execute("SELECT item_kind FROM document_items WHERE document_id=? LIMIT 1", (doc_id,)).fetchone()
    if (first_kind and first_kind["item_kind"] == "reservation") or (not has_items and doc["doc_type"] == "booking_statement"):
        return _confirm_reservations(conn, doc, doc_id, bool(has_items), included)

    if has_items:
        item_ids = request.form.getlist("item_id")
        property_ids = request.form.getlist("property_id")
        vendors = request.form.getlist("vendor")
        descriptions = request.form.getlist("description")
        amounts = request.form.getlist("amount")
        categories = request.form.getlist("category")
        years = request.form.getlist("year")
        months = request.form.getlist("month")
        for item_id, pid, vendor, desc, amount_s, category, year_s, month_s in zip(
            item_ids, property_ids, vendors, descriptions, amounts, categories, years, months
        ):
            item = conn.execute("SELECT * FROM document_items WHERE id=? AND document_id=?", (item_id, doc_id)).fetchone()
            if not item:
                continue
            include_row = item_id in included
            try:
                amount = abs(float(amount_s)) if amount_s else None
            except ValueError:
                amount = None
            direction = "income" if category == "booking_income" else "expense"
            final_value = {"property_id": pid, "vendor": vendor, "description": desc, "amount": amount,
                            "category": category, "direction": direction, "include": include_row}
            conn.execute(
                """UPDATE document_items SET property_id=?, vendor=?, raw_description=?, amount=?, category=?,
                     direction=?, include=?, reviewed=1, final_value=? WHERE id=?""",
                (pid, vendor, desc, amount, category, direction, 1 if include_row else 0, json.dumps(final_value), item_id),
            )
            if not include_row or not amount or not pid:
                continue
            year, month = int(year_s), int(month_s)
            vendor_id = get_or_create_vendor(conn, vendor)
            cur = conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, vendor_id, description, amount, direction, category, source, document_id)
                   VALUES (?,?,?,?,?,?,?,?,'upload',?)""",
                (pid, f"{year}-{month:02d}-01", vendor, vendor_id, desc, amount, direction, category, doc_id),
            )
            conn.execute("UPDATE document_items SET duplicate_of=? WHERE id=? AND duplicate_of IS NULL",
                         (cur.lastrowid, item_id))
            added += 1
            final_property_id = pid
    else:
        # Nothing was auto-extracted -- the manual 5-blank-row fallback,
        # not backed by document_items.
        property_ids = request.form.getlist("property_id")
        vendors = request.form.getlist("vendor")
        descriptions = request.form.getlist("description")
        amounts = request.form.getlist("amount")
        categories = request.form.getlist("category")
        years = request.form.getlist("year")
        months = request.form.getlist("month")
        for i, (pid, vendor, desc, amount_s, category, year_s, month_s) in enumerate(
            zip(property_ids, vendors, descriptions, amounts, categories, years, months)
        ):
            if str(i) not in included or not amount_s or not pid:
                continue
            try:
                amount = abs(float(amount_s))
            except ValueError:
                continue
            year, month = int(year_s), int(month_s)
            direction = "income" if category == "booking_income" else "expense"
            vendor_id = get_or_create_vendor(conn, vendor)
            conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, vendor_id, description, amount, direction, category, source, document_id)
                   VALUES (?,?,?,?,?,?,?,?,'upload',?)""",
                (pid, f"{year}-{month:02d}-01", vendor, vendor_id, desc, amount, direction, category, doc_id),
            )
            added += 1
            final_property_id = pid

    conn.execute("UPDATE documents SET status='confirmed', reviewed=1, property_id=? WHERE id=?",
                 (final_property_id, doc_id))
    conn.commit()
    flash(f"{added} transaction{'s' if added != 1 else ''} added.")
    return redirect(url_for("properties.detail", property_id=final_property_id) if final_property_id else url_for("documents.index"))



def _num(text):
    try:
        return abs(float(text))
    except (TypeError, ValueError):
        return None


def _confirm_reservations(conn, doc, doc_id, has_items, included):
    """Booking-statement lines become real `bookings` rows (source='upload'),
    never income transactions -- kpis.py sums both, so writing a reservation
    as both would count it twice."""
    f = request.form
    item_ids = f.getlist("item_id")
    n = len(f.getlist("check_in"))
    added, skipped, final_property_id = 0, 0, doc["property_id"]
    touched = {}  # (property_id, 'YYYY-MM') -> revenue before this upload
    for i in range(n):
        row_key = item_ids[i] if has_items else str(i)
        pid = f.getlist("property_id")[i]
        check_in, check_out = f.getlist("check_in")[i], f.getlist("check_out")[i]
        net, gross, fees = _num(f.getlist("net")[i]), _num(f.getlist("gross")[i]), _num(f.getlist("fees")[i]) or 0
        if net is None and gross is not None:
            net = max(gross - fees, 0)
        platform = f.getlist("platform")[i].strip() or None
        code = f.getlist("reservation_id")[i].strip() or None
        include_row = row_key in included
        if has_items:
            conn.execute(
                """UPDATE document_items SET property_id=?, platform=?, reservation_id=?, check_in=?, check_out=?,
                     gross_revenue=?, platform_fees=?, net_revenue=?, amount=?, include=?, reviewed=1 WHERE id=?""",
                (pid, platform, code, check_in, check_out, gross, fees, net, net, 1 if include_row else 0, row_key))
        if not include_row or not pid or not check_in or not check_out or net is None or check_out <= check_in:
            if include_row:
                skipped += 1
            continue
        key = (pid, check_in[:7])
        if key not in touched:
            y, m = map(int, key[1].split("-"))
            touched[key] = kpis.revenue(conn, pid, *kpis.month_bounds(y, m))
        conn.execute(
            """INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue,
                   platform_fees, cleaning_fee, net_revenue, status, source, document_id)
               VALUES (?,?,?,?,?,?,?,0,?,'confirmed','upload',?)""",
            (pid, platform, code, check_in, check_out, gross if gross is not None else net, fees, net, doc_id))
        added += 1
        final_property_id = pid
    conn.execute("UPDATE documents SET status='confirmed', reviewed=1, property_id=? WHERE id=?", (final_property_id, doc_id))
    conn.commit()
    flash(f"{added} reservation{'s' if added != 1 else ''} added."
          + (f" {skipped} skipped -- each needs a flat, valid dates and an amount." if skipped else ""))
    names = {p["id"]: p["name"] for p in get_properties(conn)}
    for (pid, ym), before in touched.items():
        y, m = map(int, ym.split("-"))
        after = kpis.revenue(conn, pid, *kpis.month_bounds(y, m))
        flash(f"{names.get(pid, pid)}, {MONTH_NAMES[m]} {y} is now based on the reservations on file: "
              f"£{before:,.0f} before, £{after:,.0f} now. If that looks low, the statement may not cover every "
              f"channel or reservation for the month -- upload the rest, or delete these to restore the Excel figure.")
    return redirect(url_for("bookings.index"))


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
    conn.execute("UPDATE document_items SET duplicate_of=NULL, reviewed=0 WHERE document_id=?", (doc_id,))
    n_tx = conn.execute("DELETE FROM transactions WHERE document_id=? AND source='upload'", (doc_id,)).rowcount
    n_bk = conn.execute("DELETE FROM bookings WHERE document_id=? AND source='upload'", (doc_id,)).rowcount
    conn.execute("UPDATE documents SET status='extracted', reviewed=0 WHERE id=?", (doc_id,))
    record(conn, "document", doc_id, "delete", field="import", old_value=f"{n_tx} transactions, {n_bk} reservations")
    conn.commit()
    flash(f"Import undone: removed {n_tx} transaction{'s' if n_tx != 1 else ''} and {n_bk} reservation{'s' if n_bk != 1 else ''}. "
          f"Any month that was based on those reservations goes back to its earlier figures.")
    return redirect(url_for("documents.review", doc_id=doc_id))
