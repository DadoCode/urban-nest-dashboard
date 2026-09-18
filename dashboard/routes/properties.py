import datetime
import json

from flask import Blueprint, flash, make_response, redirect, render_template, request, url_for

import db
import services.extraction as extraction
import services.ical_sync as ical_sync
import services.kpis as kpis
from services.common import (MONTH_NAMES, get_properties, get_property, pct_delta,
                              target_row, tiles_for, yoy_pairs)
from services.completeness import completeness_for, seed_defaults

bp = Blueprint("properties", __name__)


@bp.route("/properties")
def index():
    conn = db.get_conn()
    q = (request.args.get("q") or "").strip().lower()
    status = request.args.get("status", "active")
    year, month = kpis.current_period(conn)
    start, end = kpis.month_bounds(year, month)

    rows_q = "SELECT * FROM properties WHERE type != 'overhead'"
    if status == "active":
        rows_q += " AND active = 1"
    elif status == "inactive":
        rows_q += " AND active = 0"
    rows_q += " ORDER BY name"
    flats = conn.execute(rows_q).fetchall()
    if q:
        flats = [p for p in flats if q in p["name"].lower() or q in (p["address"] or "").lower()]

    rows = []
    for p in flats:
        snap = kpis.kpi_snapshot(conn, p["id"], start, end)
        target = target_row(conn, p["id"], year, month)
        rev_target = (target["revenue_target"] if target else 0) or 0
        completeness = completeness_for(conn, p["id"], start, end)
        rows.append({
            "id": p["id"], "name": p["name"], "active": p["active"],
            "revenue": snap["revenue"], "profit": snap["net_profit"],
            "occupancy": snap["occupancy"], "adr": snap["adr"], "revpar": snap["revpar"],
            "vs_target": round(snap["revenue"] / rev_target * 100, 1) if rev_target else None,
            "completeness": completeness,
        })
    rows.sort(key=lambda r: r["revenue"], reverse=True)

    return render_template(
        "properties.html", active="properties", all_properties=get_properties(conn), active_property=None,
        rows=rows, q=q, status=status, current_month=f"{MONTH_NAMES[month]} {year}",
        total_count=len(rows),
    )


def _load(conn, property_id):
    """Common lookups every tab needs: the property row, whether it's the
    overhead cost-centre, and the "current" year/month those tabs report on."""
    prop = get_property(conn, property_id)
    if not prop:
        return None, None, None, None
    is_overhead = prop["type"] == "overhead"
    year, month = kpis.current_period(conn)
    return prop, is_overhead, year, month


def _checklist(conn, property_id, is_overhead, year, month):
    has_target = bool(target_row(conn, property_id, year, month))
    has_calendar = bool(conn.execute("SELECT ical_url FROM properties WHERE id=?", (property_id,)).fetchone()["ical_url"])
    has_documents = bool(conn.execute("SELECT 1 FROM documents WHERE property_id=? LIMIT 1", (property_id,)).fetchone())
    return {"has_target": has_target, "has_calendar": has_calendar, "has_documents": has_documents}


def _remember_visit(resp, property_id):
    from app import RECENT_COOKIE, RECENT_MAX
    prior = [i for i in request.cookies.get(RECENT_COOKIE, "").split(",") if i and i != property_id]
    resp.set_cookie(RECENT_COOKIE, ",".join([property_id, *prior][:RECENT_MAX]), max_age=60 * 60 * 24 * 90)


@bp.route("/properties/<property_id>")
def detail(property_id):
    conn = db.get_conn()
    prop, is_overhead, year, month = _load(conn, property_id)
    if not prop:
        flash(f"Unknown property '{property_id}'.")
        return redirect(url_for("overview.index"))

    if is_overhead:
        start, end = kpis.month_bounds(year, month)
        pstart, pend = kpis.month_bounds(*kpis.prior_month(year, month))
        cur_cost = kpis.costs(conn, property_id, start, end)
        prev_cost = kpis.costs(conn, property_id, pstart, pend)
        tiles = [{"label": f"Total costs — {MONTH_NAMES[month]} {year}", "value": f"£{cur_cost:,.0f}",
                  "delta": pct_delta(cur_cost, prev_cost)}] if cur_cost or prev_cost else []
        goal_progress, target = None, None
    else:
        tiles, cur, prev = tiles_for(conn, property_id, year, month)
        if f"{year}-{month:02d}" not in kpis.months_with_data(conn, property_id):
            tiles = []
        target = target_row(conn, property_id, year, month)
        rev_target = (target["revenue_target"] if target else 0) or 0
        goal_progress = round(cur["revenue"] / rev_target * 100, 1) if rev_target else None

    series = kpis.monthly_series(conn, property_id)
    yoy = yoy_pairs(conn, property_id, (year, month))

    resp = make_response(render_template(
        "property/overview.html", active="properties", all_properties=get_properties(conn),
        active_property=property_id, active_tab="overview",
        prop=prop, is_overhead=is_overhead, tiles=tiles, goal=target, goal_progress=goal_progress,
        year=year, month=month, month_name=MONTH_NAMES[month], yoy=yoy,
        months_json=json.dumps([s["ym"] for s in series]),
        income_json=json.dumps([s["revenue"] for s in series]),
        profit_json=json.dumps([s["net_profit"] for s in series]),
        costs_json=json.dumps([s["costs"] for s in series]),
        occupancy_json=json.dumps([round(s["occupancy"] * 100, 1) for s in series]),
    ))
    if not is_overhead:
        _remember_visit(resp, property_id)
    return resp


@bp.route("/properties/<property_id>/bookings")
def bookings(property_id):
    conn = db.get_conn()
    prop, is_overhead, year, month = _load(conn, property_id)
    if not prop:
        flash(f"Unknown property '{property_id}'.")
        return redirect(url_for("overview.index"))
    if is_overhead:
        return redirect(url_for("properties.detail", property_id=property_id))

    start, end = kpis.month_bounds(year, month)
    rows = conn.execute(
        """SELECT *, CAST(julianday(check_out) - julianday(check_in) AS INTEGER) AS nights
           FROM bookings WHERE property_id=? AND status='confirmed' AND reservation_id != 'monthly-aggregate'
           ORDER BY check_in DESC LIMIT 100""",
        (property_id,),
    ).fetchall()

    resp = make_response(render_template(
        "property/bookings.html", active="properties", all_properties=get_properties(conn),
        active_property=property_id, active_tab="bookings",
        prop=prop, is_overhead=is_overhead, year=year, month=month, month_name=MONTH_NAMES[month],
        booked_nights=kpis.booked_nights(conn, property_id, start, end),
        adr=kpis.adr(conn, property_id, start, end),
        bookings=rows,
    ))
    _remember_visit(resp, property_id)
    return resp


@bp.route("/properties/<property_id>/expenses")
def expenses_tab(property_id):
    conn = db.get_conn()
    prop, is_overhead, year, month = _load(conn, property_id)
    if not prop:
        flash(f"Unknown property '{property_id}'.")
        return redirect(url_for("overview.index"))

    transactions = conn.execute(
        """SELECT *, CAST(strftime('%Y', date) AS INTEGER) AS year, CAST(strftime('%m', date) AS INTEGER) AS month
           FROM transactions WHERE property_id=? ORDER BY date DESC, id DESC LIMIT 60""",
        (property_id,),
    ).fetchall()

    resp = make_response(render_template(
        "property/expenses.html", active="properties", all_properties=get_properties(conn),
        active_property=property_id, active_tab="expenses",
        prop=prop, is_overhead=is_overhead, year=year, month=month, month_name=MONTH_NAMES[month],
        expenses=transactions,
    ))
    if not is_overhead:
        _remember_visit(resp, property_id)
    return resp


@bp.route("/properties/<property_id>/documents")
def documents_tab(property_id):
    conn = db.get_conn()
    prop, is_overhead, year, month = _load(conn, property_id)
    if not prop:
        flash(f"Unknown property '{property_id}'.")
        return redirect(url_for("overview.index"))

    documents = conn.execute(
        "SELECT * FROM documents WHERE property_id=? ORDER BY uploaded_at DESC", (property_id,)
    ).fetchall()

    resp = make_response(render_template(
        "property/documents.html", active="properties", all_properties=get_properties(conn),
        active_property=property_id, active_tab="documents",
        prop=prop, is_overhead=is_overhead, year=year, month=month, month_name=MONTH_NAMES[month],
        documents=documents, extraction_available=extraction.available(),
    ))
    if not is_overhead:
        _remember_visit(resp, property_id)
    return resp


@bp.route("/properties/<property_id>/settings")
def settings_tab(property_id):
    conn = db.get_conn()
    prop, is_overhead, year, month = _load(conn, property_id)
    if not prop:
        flash(f"Unknown property '{property_id}'.")
        return redirect(url_for("overview.index"))

    target = target_row(conn, property_id, year, month)
    goal_progress = None
    if not is_overhead:
        rev_target = (target["revenue_target"] if target else 0) or 0
        if rev_target:
            start, end = kpis.month_bounds(year, month)
            cur_rev = kpis.revenue(conn, property_id, start, end)
            goal_progress = round(cur_rev / rev_target * 100, 1)

    resp = make_response(render_template(
        "property/settings.html", active="properties", all_properties=get_properties(conn),
        active_property=property_id, active_tab="settings",
        prop=prop, is_overhead=is_overhead, year=year, month=month, month_name=MONTH_NAMES[month],
        goal=target, goal_progress=goal_progress,
        checklist=_checklist(conn, property_id, is_overhead, year, month),
    ))
    if not is_overhead:
        _remember_visit(resp, property_id)
    return resp


@bp.route("/property/<property_id>")
def legacy_detail(property_id):
    return redirect(url_for("properties.detail", property_id=property_id), code=301)


@bp.route("/apartments", methods=["POST"])
def add():
    conn = db.get_conn()
    name = (request.form.get("name") or "").strip()
    address = (request.form.get("address") or "").strip() or name
    if not name:
        flash("Give the new apartment a name.")
        return redirect(url_for("overview.index"))
    slug = db.unique_slug(conn, db.slugify(name))
    conn.execute("INSERT INTO properties (id, code, name, address, type) VALUES (?,?,?,?,'flat')",
                 (slug, slug.upper()[:10], name, address))
    seed_defaults(conn, slug)
    conn.commit()
    flash(f"Added {name}. Upload its first document or add an entry to get it on the board.")
    return redirect(url_for("properties.detail", property_id=slug))


@bp.route("/property/<property_id>/goal", methods=["POST"])
def update_goal(property_id):
    conn = db.get_conn()
    year, month = kpis.current_period(conn)
    try:
        revenue_target = float(request.form["income_target"])
        profit_target = float(request.form["profit_target"])
        occ_raw = request.form.get("occupancy_target", "").strip()
        occupancy_target = float(occ_raw) if occ_raw else None
    except (KeyError, ValueError):
        flash("Enter numbers for the goal fields.")
        return redirect(request.referrer or url_for("overview.index"))
    conn.execute(
        """INSERT INTO targets (property_id, year, month, revenue_target, profit_target, occupancy_target, source)
           VALUES (?,?,?,?,?,?,'manual')
           ON CONFLICT(property_id, year, month) DO UPDATE SET
             revenue_target=excluded.revenue_target, profit_target=excluded.profit_target,
             occupancy_target=excluded.occupancy_target""",
        (property_id, year, month, revenue_target, profit_target, occupancy_target),
    )
    conn.commit()
    flash("Goal updated.")
    return redirect(url_for("properties.settings_tab", property_id=property_id))


@bp.route("/property/<property_id>/sync_calendar", methods=["POST"])
def sync_calendar(property_id):
    conn = db.get_conn()
    prop = get_property(conn, property_id)
    if not prop:
        flash(f"Unknown property '{property_id}'.")
        return redirect(url_for("overview.index"))

    url = (request.form.get("ical_url") or "").strip() or prop["ical_url"]
    if not url:
        flash("Paste the calendar's export/sync URL first (Airbnb: listing → Availability → Export calendar).")
        return redirect(url_for("properties.settings_tab", property_id=property_id))

    try:
        ics_text = ical_sync.fetch(url)
        events = ical_sync.parse_events(ics_text)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("properties.settings_tab", property_id=property_id))

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
    return redirect(url_for("properties.settings_tab", property_id=property_id))


@bp.route("/property/<property_id>/expense", methods=["POST"])
def add_expense(property_id):
    conn = db.get_conn()
    today = datetime.date.today()
    try:
        amount = abs(float(request.form["amount"]))
        year = int(request.form.get("year") or today.year)
        month = int(request.form.get("month") or today.month)
    except (KeyError, ValueError):
        flash("Enter a valid amount.")
        return redirect(url_for("properties.expenses_tab", property_id=property_id))
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
    return redirect(url_for("properties.expenses_tab", property_id=property_id))
