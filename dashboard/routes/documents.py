import datetime
import json

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

import db
from services.common import get_properties, get_property
from services.documents import save_upload
from services.vendors import get_or_create_vendor

bp = Blueprint("documents", __name__)


@bp.route("/documents/<int:doc_id>/file")
def file(doc_id):
    doc = db.get_conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        abort(404)
    return send_file(doc["stored_path"], download_name=doc["filename"])


@bp.route("/documents")
def index():
    conn = db.get_conn()
    docs = conn.execute("SELECT * FROM documents ORDER BY uploaded_at DESC LIMIT 200").fetchall()
    properties_by_id = {p["id"]: p for p in get_properties(conn)}
    rows = []
    for d in docs:
        rows.append({**dict(d), "property_name": properties_by_id.get(d["property_id"], {}).get("name", "Unassigned")})
    return render_template(
        "documents.html", active="documents", all_properties=get_properties(conn), active_property=None,
        docs=rows,
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
    items = json.loads(doc["extracted_json"]) if doc["extracted_json"] else []
    today = datetime.date.today()
    year = doc["detected_year"] or today.year
    month = doc["detected_month"] or today.month
    return render_template(
        "review_document.html", active="documents", all_properties=get_properties(conn),
        active_property=doc["property_id"], prop=prop, doc=doc, items=items,
        flats=get_properties(conn, include_overhead=False), year=year, month=month,
    )


@bp.route("/documents/<int:doc_id>/confirm", methods=["POST"])
def confirm(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("Unknown document.")
        return redirect(url_for("overview.index"))

    included = set(request.form.getlist("include"))  # row indices as strings
    property_ids = request.form.getlist("property_id")
    vendors = request.form.getlist("vendor")
    descriptions = request.form.getlist("description")
    amounts = request.form.getlist("amount")
    categories = request.form.getlist("category")
    years = request.form.getlist("year")
    months = request.form.getlist("month")

    added = 0
    final_property_id = doc["property_id"]
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
    flash(f"Added {added} line item(s) to the ledger.")
    return redirect(url_for("properties.detail", property_id=final_property_id) if final_property_id else url_for("documents.index"))
