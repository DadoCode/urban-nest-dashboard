import datetime
import json

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

import db
from services.common import CATEGORIES, get_properties, get_property
from services.documents import save_upload
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
    f_status = request.args.get("d_status") or ""
    f_q = (request.args.get("d_q") or "").strip()

    clauses, params = ["1=1"], []
    if f_property:
        clauses.append("property_id=?"); params.append(f_property)
    if f_type:
        clauses.append("doc_type=?"); params.append(f_type)
    if f_status:
        clauses.append("status=?"); params.append(f_status)
    if f_q:
        clauses.append("filename LIKE ?"); params.append(f"%{f_q}%")

    docs = conn.execute(
        f"SELECT * FROM documents WHERE {' AND '.join(clauses)} ORDER BY uploaded_at DESC LIMIT 200", params
    ).fetchall()
    properties_by_id = {p["id"]: p for p in get_properties(conn)}
    rows = []
    for d in docs:
        item_stats = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM document_items WHERE document_id=? AND include=1",
            (d["id"],),
        ).fetchone()
        rows.append({**dict(d), "property_name": properties_by_id.get(d["property_id"], {}).get("name", "Unassigned"),
                     "doc_type_label": DOC_TYPE_LABELS.get(d["doc_type"], d["doc_type"] or "—"),
                     "item_count": item_stats["n"], "item_amount": item_stats["amt"]})

    counts = conn.execute(
        """SELECT
             SUM(CASE WHEN status IN ('pending','extracted') THEN 1 ELSE 0 END) needs_review,
             SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) complete,
             SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed
           FROM documents"""
    ).fetchone()

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
    is_pdf = (doc["filename"] or "").lower().endswith(".pdf")
    is_image = (doc["filename"] or "").lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    return render_template(
        "review_document.html", active="documents", all_properties=get_properties(conn),
        active_property=doc["property_id"], prop=prop, doc=doc, items=items,
        flats=get_properties(conn, include_overhead=False), year=year, month=month,
        categories=CATEGORIES, is_pdf=is_pdf, is_image=is_image,
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
