"""
Urban Nest Estates business dashboard -- sourced from the real accounts
tracker (imported by scripts/import_excel_tracker.py into data/dashboard.db)
plus whatever's added by hand or through document uploads from here on.

Run with:  python3 dashboard/app.py
"""
import datetime
import json
import mimetypes
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash

import calendar as pycalendar

import db
import extraction
import ical_sync

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = ROOT / "data" / "uploads"

app = Flask(__name__)
app.secret_key = "urban-nest-dashboard"  # local-only tool, no auth/session sensitivity

MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]


@app.before_request
def _setup():
    db.ensure_schema()
    UPLOADS.mkdir(parents=True, exist_ok=True)


def current_year_month(conn):
    """The 'current' reporting period is the latest month where at least
    half the active portfolio has real income recorded -- this is a
    hand-updated ledger, not a live feed, so the calendar's actual current
    month is usually still mostly-empty for a few weeks (one property
    entered early doesn't make it 'the current month'). Falls back to
    today's month if the ledger has no data at all yet."""
    active_count = conn.execute("SELECT COUNT(*) FROM properties WHERE active = 1 AND is_overhead = 0").fetchone()[0] or 1
    threshold = max(1, active_count // 2)
    row = conn.execute(
        """SELECT year, month FROM monthly_summary
           GROUP BY year, month HAVING COUNT(*) FILTER (WHERE income > 0) >= ?
           ORDER BY year DESC, month DESC LIMIT 1""",
        (threshold,),
    ).fetchone()
    if row:
        return row["year"], row["month"]
    today = datetime.date.today()
    return today.year, today.month


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
        clauses.append("is_overhead = 0")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY is_overhead, name"
    return conn.execute(q).fetchall()


def get_property(conn, property_id):
    return conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()


def summary_row(conn, property_id, year, month):
    return conn.execute(
        "SELECT * FROM monthly_summary WHERE property_id=? AND year=? AND month=?",
        (property_id, year, month),
    ).fetchone()


def prior_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def full_series(conn, property_id=None):
    """Chronological list of monthly rows -- one property, or the whole
    portfolio summed together when property_id is None."""
    if property_id:
        rows = conn.execute(
            "SELECT * FROM monthly_summary WHERE property_id=? ORDER BY year, month",
            (property_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    rows = conn.execute(
        """SELECT year, month,
                  SUM(income) income, SUM(total_costs) total_costs,
                  SUM(opex) opex, SUM(capex) capex,
                  SUM(net_profit) net_profit, SUM(operating_profit) operating_profit,
                  AVG(occupancy) occupancy, SUM(days_booked) days_booked
           FROM monthly_summary GROUP BY year, month ORDER BY year, month"""
    ).fetchall()
    return [dict(r) for r in rows]


def yoy_pairs(series, current_period):
    by_key = {(r["year"], r["month"]): r for r in series}
    pairs = []
    for (year, month), row in sorted(by_key.items()):
        if (year, month) > current_period:
            continue
        prev = by_key.get((year - 1, month))
        if prev and (row["income"] or prev["income"]):
            pairs.append({
                "label": f"{MONTH_NAMES[month]} {year}",
                "this_year": row["income"] or 0,
                "last_year": prev["income"] or 0,
                "delta_pct": pct_delta(row["income"] or 0, prev["income"] or 0),
            })
    return pairs


def goal_row(conn, property_id, year, month):
    return conn.execute(
        "SELECT * FROM goals WHERE property_id=? AND year=? AND month=?",
        (property_id, year, month),
    ).fetchone()


def recompute_derived_month(conn, property_id, year, month):
    """Keeps monthly_summary current for months that aren't in the Excel
    tracker at all -- once uploads/manual entries are the only source for a
    month, this recomputes its income/costs/profit from expense_items every
    time a new line item lands. Never touches a month that has a real
    'excel_import' row: that ledger is the trusted historical figure and
    itemized purchases don't reconstruct it exactly (see the import
    script's docstring)."""
    existing = conn.execute(
        "SELECT source, income, total_costs FROM monthly_summary WHERE property_id=? AND year=? AND month=?",
        (property_id, year, month),
    ).fetchone()
    # The Excel import pre-fills a full 12-month template per year, so a
    # future month often already has a zeroed-out placeholder row -- that's
    # not real historical data, so it's still fair game to derive over.
    if existing and existing["source"] == "excel_import" and (existing["income"] or existing["total_costs"]):
        return
    row = conn.execute(
        """SELECT
             COALESCE(SUM(amount) FILTER (WHERE category = 'booking_income'), 0) income,
             COALESCE(SUM(amount) FILTER (WHERE category != 'booking_income'), 0) costs
           FROM expense_items WHERE property_id=? AND year=? AND month=?""",
        (property_id, year, month),
    ).fetchone()
    income, costs = row["income"], row["costs"]
    conn.execute(
        """INSERT INTO monthly_summary (property_id, year, month, income, total_costs, net_profit, source)
           VALUES (?,?,?,?,?,?,'derived')
           ON CONFLICT(property_id, year, month) DO UPDATE SET
             income=excluded.income, total_costs=excluded.total_costs,
             net_profit=excluded.net_profit, source='derived'""",
        (property_id, year, month, income, -costs, income - costs),
    )


def merge_calendar_month(conn, property_id, year, month, nights_booked):
    """Writes occupancy/days_booked from a synced booking calendar without
    touching income/costs (the calendar only tells us which nights were
    booked, not what they were worth) and without overwriting a month that
    already has a real recorded occupancy figure from the Excel import."""
    days_in_month = pycalendar.monthrange(year, month)[1]
    occupancy = min(nights_booked / days_in_month, 1.0)
    existing = conn.execute(
        "SELECT source, occupancy FROM monthly_summary WHERE property_id=? AND year=? AND month=?",
        (property_id, year, month),
    ).fetchone()
    if existing and existing["source"] == "excel_import" and existing["occupancy"]:
        return False
    conn.execute(
        """INSERT INTO monthly_summary (property_id, year, month, occupancy, days_booked, source)
           VALUES (?,?,?,?,?,'ical')
           ON CONFLICT(property_id, year, month) DO UPDATE SET
             occupancy=excluded.occupancy, days_booked=excluded.days_booked,
             source=CASE WHEN monthly_summary.source = 'excel_import' THEN monthly_summary.source ELSE 'ical' END""",
        (property_id, year, month, occupancy, nights_booked),
    )
    return True


@app.route("/")
def index():
    conn = db.get_conn()
    nav_properties = get_properties(conn)
    flats = get_properties(conn, include_overhead=False)
    year, month = current_year_month(conn)
    py, pm = prior_month(year, month)

    portfolio_series = full_series(conn)
    by_key = {(r["year"], r["month"]): r for r in portfolio_series}
    current = by_key.get((year, month))
    previous = by_key.get((py, pm))

    tiles = []
    if current:
        tiles.append({"label": f"Revenue — {MONTH_NAMES[month]} {year}", "value": f"£{(current['income'] or 0):,.0f}",
                       "delta": pct_delta(current["income"] or 0, previous["income"] or 0) if previous else None})
        tiles.append({"label": "Net profit", "value": f"£{(current['net_profit'] or 0):,.0f}",
                       "delta": pct_delta(current["net_profit"] or 0, previous["net_profit"] or 0) if previous else None})
        tiles.append({"label": "Occupancy (avg)", "value": f"{(current['occupancy'] or 0) * 100:.0f}%",
                       "delta": pct_delta(current["occupancy"] or 0, previous["occupancy"] or 0) if previous else None})
        tiles.append({"label": "Costs", "value": f"£{abs(current['total_costs'] or 0):,.0f}",
                       "delta": pct_delta(abs(current["total_costs"] or 0), abs(previous["total_costs"] or 0)) if previous else None})

    goals_this_month = conn.execute(
        "SELECT SUM(income_target) t, SUM(profit_target) p FROM goals WHERE year=? AND month=?", (year, month)
    ).fetchone()
    income_goal = goals_this_month["t"] or 0
    goal_progress = round((current["income"] or 0) / income_goal * 100, 1) if current and income_goal else 0

    prop_rows = []
    for p in flats:
        row = summary_row(conn, p["id"], year, month)
        g = goal_row(conn, p["id"], year, month)
        income = (row["income"] if row else 0) or 0
        target = (g["income_target"] if g else 0) or 0
        prop_rows.append({
            "id": p["id"], "name": p["name"],
            "income": income,
            "profit": (row["net_profit"] if row else 0) or 0,
            "occupancy": (row["occupancy"] if row else 0) or 0,
            "target": target,
            "progress": round(income / target * 100, 1) if target else None,
        })
    prop_rows.sort(key=lambda r: r["income"], reverse=True)

    yoy = yoy_pairs(portfolio_series, (year, month))

    return render_template(
        "index.html", active="overview", all_properties=nav_properties, flats_count=len(flats),
        active_property=None, tiles=tiles, current_month=f"{MONTH_NAMES[month]} {year}",
        income_goal=income_goal, goal_progress=goal_progress, prop_rows=prop_rows, yoy=yoy,
        months_json=json.dumps([f"{r['year']}-{r['month']:02d}" for r in portfolio_series]),
        income_json=json.dumps([r["income"] or 0 for r in portfolio_series]),
        profit_json=json.dumps([r["net_profit"] or 0 for r in portfolio_series]),
        occupancy_json=json.dumps([round((r["occupancy"] or 0) * 100, 1) for r in portfolio_series]),
    )


@app.route("/property/<property_id>")
def property_page(property_id):
    conn = db.get_conn()
    prop = get_property(conn, property_id)
    if not prop:
        flash(f"Unknown property '{property_id}'.")
        return redirect(url_for("index"))

    year, month = current_year_month(conn)
    py, pm = prior_month(year, month)
    series = full_series(conn, property_id)
    by_key = {(r["year"], r["month"]): r for r in series}
    current = by_key.get((year, month))
    previous = by_key.get((py, pm))

    tiles = []
    if current:
        tiles.append({"label": f"Revenue — {MONTH_NAMES[month]} {year}", "value": f"£{(current['income'] or 0):,.0f}",
                       "delta": pct_delta(current["income"] or 0, previous["income"] or 0) if previous else None})
        tiles.append({"label": "Net profit", "value": f"£{(current['net_profit'] or 0):,.0f}",
                       "delta": pct_delta(current["net_profit"] or 0, previous["net_profit"] or 0) if previous else None})
        tiles.append({"label": "Occupancy", "value": f"{(current['occupancy'] or 0) * 100:.0f}%",
                       "delta": pct_delta(current["occupancy"] or 0, previous["occupancy"] or 0) if previous else None})
        tiles.append({"label": "Days booked", "value": int(current["days_booked"] or 0),
                       "delta": pct_delta(current["days_booked"] or 0, previous["days_booked"] or 0) if previous else None})

    goal = goal_row(conn, property_id, year, month)
    income = (current["income"] or 0) if current else 0
    target = (goal["income_target"] if goal else 0) or 0
    goal_progress = round(income / target * 100, 1) if target else None

    expenses = conn.execute(
        """SELECT * FROM expense_items WHERE property_id=?
           ORDER BY year DESC, month DESC, id DESC LIMIT 60""",
        (property_id,),
    ).fetchall()

    documents = conn.execute(
        "SELECT * FROM documents WHERE property_id=? ORDER BY uploaded_at DESC", (property_id,)
    ).fetchall()

    yoy = yoy_pairs(series, (year, month))

    return render_template(
        "property.html", active="property", all_properties=get_properties(conn),
        active_property=property_id, prop=prop, tiles=tiles, goal=goal,
        goal_progress=goal_progress, year=year, month=month, month_name=MONTH_NAMES[month],
        expenses=expenses, documents=documents, yoy=yoy,
        extraction_available=extraction.available(),
        months_json=json.dumps([f"{r['year']}-{r['month']:02d}" for r in series]),
        income_json=json.dumps([r["income"] or 0 for r in series]),
        profit_json=json.dumps([r["net_profit"] or 0 for r in series]),
        costs_json=json.dumps([abs(r["total_costs"] or 0) for r in series]),
        occupancy_json=json.dumps([round((r["occupancy"] or 0) * 100, 1) for r in series]),
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
    conn.execute("INSERT INTO properties (id, code, name, address) VALUES (?,?,?,?)",
                 (slug, slug.upper()[:10], name, address))
    conn.commit()
    flash(f"Added {name}. Upload its first document or add an entry to get it on the board.")
    return redirect(url_for("property_page", property_id=slug))


@app.route("/property/<property_id>/goal", methods=["POST"])
def update_goal(property_id):
    conn = db.get_conn()
    year, month = current_year_month(conn)
    try:
        income_target = float(request.form["income_target"])
        profit_target = float(request.form["profit_target"])
    except (KeyError, ValueError):
        flash("Enter numbers for the goal fields.")
        return redirect(request.referrer or url_for("index"))
    conn.execute(
        """INSERT INTO goals (property_id, year, month, income_target, profit_target, source)
           VALUES (?,?,?,?,?,'manual')
           ON CONFLICT(property_id, year, month) DO UPDATE SET
             income_target=excluded.income_target, profit_target=excluded.profit_target""",
        (property_id, year, month, income_target, profit_target),
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
        nights = ical_sync.sync(url)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("property_page", property_id=property_id))

    conn.execute(
        "UPDATE properties SET ical_url=?, ical_synced_at=datetime('now') WHERE id=?",
        (url, property_id),
    )
    updated = 0
    for (year, month), n in nights.items():
        if merge_calendar_month(conn, property_id, year, month, n):
            updated += 1
    conn.commit()
    skipped = len(nights) - updated
    msg = f"Synced occupancy for {updated} month(s) from the calendar."
    if skipped:
        msg += f" ({skipped} month(s) already had a recorded figure and were left alone.)"
    flash(msg)
    return redirect(url_for("property_page", property_id=property_id))


@app.route("/property/<property_id>/expense", methods=["POST"])
def add_expense(property_id):
    conn = db.get_conn()
    default_year, default_month = current_year_month(conn)
    try:
        amount = abs(float(request.form["amount"]))
        year = int(request.form.get("year") or default_year)
        month = int(request.form.get("month") or default_month)
    except (KeyError, ValueError):
        flash("Enter a valid amount.")
        return redirect(url_for("property_page", property_id=property_id))
    conn.execute(
        """INSERT INTO expense_items (property_id, year, month, vendor, description, amount, category, source)
           VALUES (?,?,?,?,?,?,?,'manual')""",
        (property_id, year, month, request.form.get("vendor", ""), request.form.get("description", ""),
         amount, request.form.get("category", "purchase")),
    )
    recompute_derived_month(conn, property_id, year, month)
    conn.commit()
    flash("Expense added.")
    return redirect(url_for("property_page", property_id=property_id))


@app.route("/property/<property_id>/upload", methods=["POST"])
def upload_document(property_id):
    conn = db.get_conn()
    file = request.files.get("document")
    if not file or not file.filename:
        flash("Choose a file first.")
        return redirect(url_for("property_page", property_id=property_id))

    dest_dir = UPLOADS / property_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{Path(file.filename).name}"
    dest_path = dest_dir / safe_name
    file.save(dest_path)

    mime_type = file.mimetype or mimetypes.guess_type(file.filename)[0]
    cur = conn.execute(
        "INSERT INTO documents (property_id, filename, stored_path, doc_type, status) VALUES (?,?,?,?,'pending')",
        (property_id, file.filename, str(dest_path), request.form.get("doc_type", "other")),
    )
    doc_id = cur.lastrowid
    conn.commit()

    items = extraction.extract(dest_path, mime_type)
    if items is not None:
        conn.execute("UPDATE documents SET status='extracted', extracted_json=? WHERE id=?",
                     (json.dumps(items), doc_id))
        conn.commit()
        flash(f"Extracted {len(items)} line item(s) — review and confirm below.")
    else:
        flash("Saved the file. No extraction key configured, so add its line items by hand below.")

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
    year, month = today.year, today.month
    return render_template(
        "review_document.html", active="property", all_properties=get_properties(conn),
        active_property=doc["property_id"], prop=prop, doc=doc, items=items,
        year=year, month=month,
    )


@app.route("/documents/<int:doc_id>/confirm", methods=["POST"])
def confirm_document(doc_id):
    conn = db.get_conn()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("Unknown document.")
        return redirect(url_for("index"))

    vendors = request.form.getlist("vendor")
    descriptions = request.form.getlist("description")
    amounts = request.form.getlist("amount")
    categories = request.form.getlist("category")
    years = request.form.getlist("year")
    months = request.form.getlist("month")

    added = 0
    touched_periods = set()
    for vendor, desc, amount_s, category, year_s, month_s in zip(vendors, descriptions, amounts, categories, years, months):
        if not amount_s:
            continue
        try:
            amount = abs(float(amount_s))
        except ValueError:
            continue
        year, month = int(year_s), int(month_s)
        conn.execute(
            """INSERT INTO expense_items (property_id, year, month, vendor, description, amount, category, source, document_id)
               VALUES (?,?,?,?,?,?,?,'upload',?)""",
            (doc["property_id"], year, month, vendor, desc, amount, category, doc_id),
        )
        added += 1
        touched_periods.add((year, month))

    for year, month in touched_periods:
        recompute_derived_month(conn, doc["property_id"], year, month)

    conn.execute("UPDATE documents SET status='confirmed' WHERE id=?", (doc_id,))
    conn.commit()
    flash(f"Added {added} line item(s) to the ledger.")
    return redirect(url_for("property_page", property_id=doc["property_id"]))


if __name__ == "__main__":
    app.run(debug=True, port=5050)
