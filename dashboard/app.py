"""
Urban Nest Estates business dashboard.

Every KPI on every page is derived on read from `bookings` + `transactions`
via dashboard/kpis.py -- there is no cached totals table to keep in sync.
See scripts/migrate_to_normalized_schema.py for how the original Excel
import's monthly_summary/expense_items/goals became this shape.

Run with:  python3 dashboard/app.py
"""
import datetime
import json
import mimetypes
import re
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash

import db
import extraction
import ical_sync
import kpis

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = ROOT / "data" / "uploads"

app = Flask(__name__)
app.secret_key = "urban-nest-dashboard"  # local-only tool, no auth/session sensitivity

MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@app.before_request
def _setup():
    db.ensure_schema()
    UPLOADS.mkdir(parents=True, exist_ok=True)


def pct_delta(current, previous):
    if not previous:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def get_properties(conn, active_only=True, include_overhead=True):
    q = "SELECT * FROM properties"
    clauses = []
    if active_only:
        clauses.append("active = 1")
    if not include_overhead:
        clauses.append("type != 'overhead'")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY (type = 'overhead'), name"
    return conn.execute(q).fetchall()


def get_property(conn, property_id):
    return conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()


def target_row(conn, property_id, year, month):
    return conn.execute(
        "SELECT * FROM targets WHERE property_id=? AND year=? AND month=?",
        (property_id, year, month),
    ).fetchone()


def yoy_pairs(conn, property_id, current_period):
    """[(label, this_year, last_year, delta_pct), ...] for every month up to
    current_period where the same month exists a year earlier too."""
    series = {(s["year"], s["month"]): s for s in kpis.monthly_series(conn, property_id)}
    pairs = []
    for (year, month), row in sorted(series.items()):
        if (year, month) > current_period:
            continue
        prev = series.get((year - 1, month))
        if prev:
            pairs.append({
                "label": f"{MONTH_NAMES[month]} {year}",
                "this_year": row["revenue"],
                "last_year": prev["revenue"],
                "delta_pct": pct_delta(row["revenue"], prev["revenue"]),
            })
    return pairs


def tiles_for(conn, property_id, year, month, extra=None):
    py, pm = kpis.prior_month(year, month)
    start, end = kpis.month_bounds(year, month)
    pstart, pend = kpis.month_bounds(py, pm)
    cur = kpis.kpi_snapshot(conn, property_id, start, end)
    prev = kpis.kpi_snapshot(conn, property_id, pstart, pend)
    tiles = [
        {"label": f"Revenue — {MONTH_NAMES[month]} {year}", "value": f"£{cur['revenue']:,.0f}",
         "delta": pct_delta(cur["revenue"], prev["revenue"])},
        {"label": "Net profit", "value": f"£{cur['net_profit']:,.0f}",
         "delta": pct_delta(cur["net_profit"], prev["net_profit"])},
        {"label": "Occupancy", "value": f"{cur['occupancy'] * 100:.0f}%",
         "delta": pct_delta(cur["occupancy"], prev["occupancy"])},
        {"label": "Booked nights", "value": cur["booked_nights"],
         "delta": pct_delta(cur["booked_nights"], prev["booked_nights"])},
    ]
    return tiles, cur, prev


@app.route("/")
def index():
    conn = db.get_conn()
    nav_properties = get_properties(conn)
    flats = get_properties(conn, include_overhead=False)
    year, month = kpis.current_period(conn)
    tiles, cur, prev = tiles_for(conn, None, year, month)

    targets_total = conn.execute(
        "SELECT SUM(revenue_target) t FROM targets WHERE year=? AND month=?", (year, month)
    ).fetchone()["t"] or 0
    goal_progress = round(cur["revenue"] / targets_total * 100, 1) if targets_total else 0

    start, end = kpis.month_bounds(year, month)
    prop_rows = []
    for p in flats:
        snap = kpis.kpi_snapshot(conn, p["id"], start, end)
        t = target_row(conn, p["id"], year, month)
        target = (t["revenue_target"] if t else 0) or 0
        prop_rows.append({
            "id": p["id"], "name": p["name"],
            "income": snap["revenue"], "profit": snap["net_profit"], "occupancy": snap["occupancy"],
            "target": target,
            "progress": round(snap["revenue"] / target * 100, 1) if target else None,
        })
    prop_rows.sort(key=lambda r: r["income"], reverse=True)

    portfolio_series = kpis.monthly_series(conn, None)
    yoy = yoy_pairs(conn, None, (year, month))

    return render_template(
        "index.html", active="overview", all_properties=nav_properties, flats_count=len(flats),
        active_property=None, tiles=tiles, current_month=f"{MONTH_NAMES[month]} {year}",
        income_goal=targets_total, goal_progress=goal_progress, prop_rows=prop_rows, yoy=yoy,
        months_json=json.dumps([s["ym"] for s in portfolio_series]),
        income_json=json.dumps([s["revenue"] for s in portfolio_series]),
        profit_json=json.dumps([s["net_profit"] for s in portfolio_series]),
        occupancy_json=json.dumps([round(s["occupancy"] * 100, 1) for s in portfolio_series]),
    )


@app.route("/occupancy")
def occupancy_page():
    conn = db.get_conn()
    flats = get_properties(conn, include_overhead=False)
    year, month = kpis.current_period(conn)
    py, pm = kpis.prior_month(year, month)
    months = kpis.months_with_data(conn, None)

    series_by_property = {}
    rows = []
    for p in flats:
        own_months = set(kpis.months_with_data(conn, p["id"]))
        values = []
        for ym in months:
            if ym not in own_months:
                values.append(None)
                continue
            y, m = map(int, ym.split("-"))
            s, e = kpis.month_bounds(y, m)
            values.append(round(kpis.occupancy(conn, p["id"], s, e) * 100, 1))
        series_by_property[p["name"]] = values

        cstart, cend = kpis.month_bounds(year, month)
        pstart, pend = kpis.month_bounds(py, pm)
        cur_occ = kpis.occupancy(conn, p["id"], cstart, cend)
        prev_occ = kpis.occupancy(conn, p["id"], pstart, pend)
        rows.append({
            "id": p["id"], "name": p["name"], "occupancy": cur_occ,
            "days_booked": kpis.booked_nights(conn, p["id"], cstart, cend),
            "delta": pct_delta(cur_occ, prev_occ) if f"{py}-{pm:02d}" in own_months else None,
        })
    rows.sort(key=lambda r: r["occupancy"], reverse=True)

    portfolio_months = set(kpis.months_with_data(conn, None))
    portfolio_series = []
    for ym in months:
        if ym not in portfolio_months:
            portfolio_series.append(None)
            continue
        y, m = map(int, ym.split("-"))
        s, e = kpis.month_bounds(y, m)
        portfolio_series.append(round(kpis.occupancy(conn, None, s, e) * 100, 1))

    return render_template(
        "occupancy.html", active="occupancy", all_properties=get_properties(conn), active_property=None,
        current_month=f"{MONTH_NAMES[month]} {year}", rows=rows,
        months_json=json.dumps(months), portfolio_json=json.dumps(portfolio_series),
        series_json=json.dumps(series_by_property),
    )


@app.route("/opex-capex")
def opex_capex_page():
    conn = db.get_conn()
    flats = get_properties(conn, include_overhead=False)
    year, month = kpis.current_period(conn)
    start, end = kpis.month_bounds(year, month)
    months = kpis.months_with_data(conn, None)

    rows = []
    for p in flats:
        s, e = kpis.month_bounds(year, month)
        opex = kpis.costs(conn, p["id"], s, e, capex=False)
        capex = kpis.costs(conn, p["id"], s, e, capex=True)
        rows.append({"id": p["id"], "name": p["name"], "opex": opex, "capex": capex, "total_costs": opex + capex})
    rows.sort(key=lambda r: r["total_costs"], reverse=True)

    opex_series, capex_series = [], []
    for ym in months:
        y, m = map(int, ym.split("-"))
        s, e = kpis.month_bounds(y, m)
        opex_series.append(kpis.costs(conn, None, s, e, capex=False))
        capex_series.append(kpis.costs(conn, None, s, e, capex=True))

    def category_breakdown(start=None, end=None):
        clause, params = ("AND date>=? AND date<?", (start, end)) if start else ("", ())
        return conn.execute(
            f"""SELECT category, SUM(amount) amt, COUNT(*) n FROM transactions
                WHERE direction='expense' AND category != 'reconciliation' {clause}
                GROUP BY category ORDER BY amt DESC""",
            params,
        ).fetchall()

    return render_template(
        "opex_capex.html", active="opex_capex", all_properties=get_properties(conn), active_property=None,
        current_month=f"{MONTH_NAMES[month]} {year}", rows=rows,
        this_month_categories=category_breakdown(start, end), all_time_categories=category_breakdown(),
        months_json=json.dumps(months), opex_json=json.dumps(opex_series), capex_json=json.dumps(capex_series),
    )


@app.route("/property/<property_id>")
def property_page(property_id):
    conn = db.get_conn()
    prop = get_property(conn, property_id)
    if not prop:
        flash(f"Unknown property '{property_id}'.")
        return redirect(url_for("index"))

    year, month = kpis.current_period(conn)
    tiles, cur, prev = tiles_for(conn, property_id, year, month)
    if f"{year}-{month:02d}" not in kpis.months_with_data(conn, property_id):
        tiles = []

    target = target_row(conn, property_id, year, month)
    rev_target = (target["revenue_target"] if target else 0) or 0
    goal_progress = round(cur["revenue"] / rev_target * 100, 1) if rev_target else None

    transactions = conn.execute(
        """SELECT *, CAST(strftime('%Y', date) AS INTEGER) AS year, CAST(strftime('%m', date) AS INTEGER) AS month
           FROM transactions WHERE property_id=? ORDER BY date DESC, id DESC LIMIT 60""",
        (property_id,),
    ).fetchall()
    documents = conn.execute(
        "SELECT * FROM documents WHERE property_id=? ORDER BY uploaded_at DESC", (property_id,)
    ).fetchall()

    series = kpis.monthly_series(conn, property_id)
    yoy = yoy_pairs(conn, property_id, (year, month))

    return render_template(
        "property.html", active="property", all_properties=get_properties(conn),
        active_property=property_id, prop=prop, tiles=tiles, goal=target,
        goal_progress=goal_progress, year=year, month=month, month_name=MONTH_NAMES[month],
        expenses=transactions, documents=documents, yoy=yoy,
        extraction_available=extraction.available(),
        months_json=json.dumps([s["ym"] for s in series]),
        income_json=json.dumps([s["revenue"] for s in series]),
        profit_json=json.dumps([s["net_profit"] for s in series]),
        costs_json=json.dumps([s["costs"] for s in series]),
        occupancy_json=json.dumps([round(s["occupancy"] * 100, 1) for s in series]),
    )


@app.route("/apartments", methods=["POST"])
def add_apartment():
    conn = db.get_conn()
    name = (request.form.get("name") or "").strip()
    address = (request.form.get("address") or "").strip() or name
    if not name:
        flash("Give the new apartment a name.")
        return redirect(url_for("index"))
    slug = db.unique_slug(conn, db.slugify(name))
    conn.execute("INSERT INTO properties (id, code, name, address, type) VALUES (?,?,?,?,'flat')",
                 (slug, slug.upper()[:10], name, address))
    conn.commit()
    flash(f"Added {name}. Upload its first document or add an entry to get it on the board.")
    return redirect(url_for("property_page", property_id=slug))


@app.route("/property/<property_id>/goal", methods=["POST"])
def update_goal(property_id):
    conn = db.get_conn()
    year, month = kpis.current_period(conn)
    try:
        revenue_target = float(request.form["income_target"])
        profit_target = float(request.form["profit_target"])
    except (KeyError, ValueError):
        flash("Enter numbers for the goal fields.")
        return redirect(request.referrer or url_for("index"))
    conn.execute(
        """INSERT INTO targets (property_id, year, month, revenue_target, profit_target, source)
           VALUES (?,?,?,?,?,'manual')
           ON CONFLICT(property_id, year, month) DO UPDATE SET
             revenue_target=excluded.revenue_target, profit_target=excluded.profit_target""",
        (property_id, year, month, revenue_target, profit_target),
    )
    conn.commit()
    flash("Goal updated.")
    return redirect(url_for("property_page", property_id=property_id))


@app.route("/property/<property_id>/sync_calendar", methods=["POST"])
def sync_calendar(property_id):
    conn = db.get_conn()
    prop = get_property(conn, property_id)
    if not prop:
        flash(f"Unknown property '{property_id}'.")
        return redirect(url_for("index"))

    url = (request.form.get("ical_url") or "").strip() or prop["ical_url"]
    if not url:
        flash("Paste the calendar's export/sync URL first (Airbnb: listing → Availability → Export calendar).")
        return redirect(url_for("property_page", property_id=property_id))

    try:
        ics_text = ical_sync.fetch(url)
        events = ical_sync.parse_events(ics_text)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("property_page", property_id=property_id))

    conn.execute("UPDATE properties SET ical_url=?, ical_synced_at=datetime('now') WHERE id=?", (url, property_id))
    added = skipped = 0
    for check_in, check_out in events:
        exists = conn.execute(
            "SELECT 1 FROM bookings WHERE property_id=? AND check_in=? AND check_out=? AND source='ical'",
            (property_id, check_in.isoformat(), check_out.isoformat()),
        ).fetchone()
        if exists:
            skipped += 1
            continue
        conn.execute(
            """INSERT INTO bookings (property_id, platform, check_in, check_out,
                                      gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source)
               VALUES (?,'airbnb',?,?,0,0,0,0,'confirmed','ical')""",
            (property_id, check_in.isoformat(), check_out.isoformat()),
        )
        added += 1
    conn.commit()
    flash(f"Synced calendar: {added} new reservation(s) added" + (f", {skipped} already on file." if skipped else "."))
    return redirect(url_for("property_page", property_id=property_id))


@app.route("/property/<property_id>/expense", methods=["POST"])
def add_expense(property_id):
    conn = db.get_conn()
    today = datetime.date.today()
    try:
        amount = abs(float(request.form["amount"]))
        year = int(request.form.get("year") or today.year)
        month = int(request.form.get("month") or today.month)
    except (KeyError, ValueError):
        flash("Enter a valid amount.")
        return redirect(url_for("property_page", property_id=property_id))
    category = request.form.get("category", "purchase")
    direction = "income" if category == "booking_income" else "expense"
    conn.execute(
        """INSERT INTO transactions (property_id, date, vendor, description, amount, direction, category, source)
           VALUES (?,?,?,?,?,?,?,'manual')""",
        (property_id, f"{year}-{month:02d}-01", request.form.get("vendor", ""),
         request.form.get("description", ""), amount, direction, category),
    )
    conn.commit()
    flash("Expense added.")
    return redirect(url_for("property_page", property_id=property_id))


def _period_from_hint(hint):
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


def _save_upload(conn, file, doc_type, property_id):
    """Handles a single uploaded file: saves it, extracts line items,
    guesses a property when none was given, flags likely duplicates.
    Returns the new document id."""
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

    period = _period_from_hint(result.get("period_hint"))
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


@app.route("/documents")
def documents_inbox():
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


@app.route("/documents/upload", methods=["POST"])
def upload_to_inbox():
    conn = db.get_conn()
    files = request.files.getlist("document")
    files = [f for f in files if f and f.filename]
    if not files:
        flash("Choose at least one file first.")
        return redirect(url_for("documents_inbox"))
    doc_type = request.form.get("doc_type", "other")
    property_id = request.form.get("property_id") or None
    last_doc_id = None
    for file in files:
        last_doc_id = _save_upload(conn, file, doc_type, property_id)
    if len(files) == 1:
        return redirect(url_for("review_document", doc_id=last_doc_id))
    return redirect(url_for("documents_inbox"))


@app.route("/property/<property_id>/upload", methods=["POST"])
def upload_document(property_id):
    conn = db.get_conn()
    file = request.files.get("document")
    if not file or not file.filename:
        flash("Choose a file first.")
        return redirect(url_for("property_page", property_id=property_id))
    doc_id = _save_upload(conn, file, request.form.get("doc_type", "other"), property_id)
    return redirect(url_for("review_document", doc_id=doc_id))


@app.route("/documents/<int:doc_id>/review")
def review_document(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("Unknown document.")
        return redirect(url_for("index"))
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


@app.route("/documents/<int:doc_id>/confirm", methods=["POST"])
def confirm_document(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("Unknown document.")
        return redirect(url_for("index"))

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
        conn.execute(
            """INSERT INTO transactions (property_id, date, vendor, description, amount, direction, category, source, document_id)
               VALUES (?,?,?,?,?,?,?,'upload',?)""",
            (pid, f"{year}-{month:02d}-01", vendor, desc, amount, direction, category, doc_id),
        )
        added += 1
        final_property_id = pid

    conn.execute("UPDATE documents SET status='confirmed', reviewed=1, property_id=? WHERE id=?",
                 (final_property_id, doc_id))
    conn.commit()
    flash(f"Added {added} line item(s) to the ledger.")
    return redirect(url_for("property_page", property_id=final_property_id) if final_property_id else url_for("documents_inbox"))


if __name__ == "__main__":
    app.run(debug=True, port=5050)
