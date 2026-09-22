import datetime
import json

from flask import Blueprint, flash, make_response, redirect, render_template, request, url_for

import db
import services.extraction as extraction
import services.ical_sync as ical_sync
import services.kpis as kpis
from services.common import MONTH_NAMES, adjusted_yoy_pairs, get_properties, get_property, pct_delta
from services.completeness import completeness_for, health_for, health_state, seed_defaults
from services.context import compare_bounds, link_params, range_params, request_context
from services.vendors import get_or_create_vendor

bp = Blueprint("properties", __name__)


@bp.route("/properties")
def index():
    conn = db.get_conn()
    q = (request.args.get("q") or "").strip().lower()
    status = request.args.get("status", "active")
    ctx = request_context(conn)
    # This page has no property selector (hide_property=True below) and
    # always lists every property -- a stray "property" left over from
    # browsing elsewhere shouldn't make "Reset to latest" appear here.
    ctx["is_latest"] = ctx["period_is_latest"] and ctx["compare"] == "previous_period"
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])

    rows_q = "SELECT * FROM properties WHERE type != 'overhead'"
    if status == "active":
        rows_q += " AND active = 1"
    elif status == "inactive":
        rows_q += " AND active = 0"
    rows_q += " ORDER BY name"
    flats = conn.execute(rows_q).fetchall()
    if q:
        flats = [p for p in flats if q in p["name"].lower() or q in (p["address"] or "").lower()]

    single = (ctx["start_year"], ctx["start_month"]) == (ctx["end_year"], ctx["end_month"])
    period_word = MONTH_NAMES[ctx["end_month"]] if single else ctx["display"]
    sort = request.args.get("sort", "revenue")
    if sort not in ("revenue", "profit", "occupancy", "adr", "name"):
        sort = "revenue"
    rows = []
    for p in flats:
        # adjusted_kpi_snapshot(): Revenue/Net profit are what this business
        # actually earns -- full figures for an owned flat, the fee share
        # for a managed one. Occupancy/ADR/RevPAR describe the flat itself
        # and are unaffected.
        snap = kpis.adjusted_kpi_snapshot(conn, p["id"], start, end)
        kind, label = health_state(health_for(conn, p["id"], start, end), period_word)
        fee = p["management_fee_pct"]
        rows.append({
            "id": p["id"], "name": p["name"], "active": p["active"],
            "revenue": snap["revenue"], "profit": snap["net_profit"],
            "occupancy": snap["occupancy"], "adr": snap["adr"], "revpar": snap["revpar"],
            "health_kind": kind, "health_label": label,
            "managed": bool(fee), "fee": fee,
        })
    rows.sort(key=(lambda r: r["name"].lower()) if sort == "name" else (lambda r: r[sort]), reverse=(sort != "name"))

    return render_template(
        "properties.html", active="properties", all_properties=get_properties(conn), active_property=None,
        rows=rows, q=q, status=status, sort=sort, current_month=ctx["display"], total_count=len(rows),
        context_bar=True, ctx=ctx, hide_property=True,
    )


def _load(conn, property_id):
    """Common lookups every tab needs: the property row, whether it's the
    overhead cost-centre, and the shared period/compare context with the
    property fixed to this one."""
    prop = get_property(conn, property_id)
    if not prop:
        return None, None, None
    ctx = request_context(conn, fixed_property=property_id)
    return prop, prop["type"] == "overhead", ctx


def _ws(ctx, prop, tab, **extra):
    """Template variables every workspace tab shares."""
    return {"active": "properties", "active_property": prop["id"], "active_tab": tab, "prop": prop,
            "context_bar": True, "ctx": ctx, "fixed_property": prop, "hide_property": True,
            "year": ctx["end_year"], "month": ctx["end_month"], "month_name": MONTH_NAMES[ctx["end_month"]], **extra}


def cx_url(endpoint, **values):
    return url_for(endpoint, **link_params(**values))


def health_title(ctx):
    single = (ctx["start_year"], ctx["start_month"]) == (ctx["end_year"], ctx["end_month"])
    return f"{MONTH_NAMES[ctx['end_month']]} data" if single else f"{ctx['display']} data"


def _range(ctx):
    return kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])


def _checklist(conn, property_id, is_overhead, year, month):
    has_calendar = bool(conn.execute("SELECT ical_url FROM properties WHERE id=?", (property_id,)).fetchone()["ical_url"])
    has_documents = bool(conn.execute("SELECT 1 FROM documents WHERE property_id=? LIMIT 1", (property_id,)).fetchone())
    return {"has_calendar": has_calendar, "has_documents": has_documents}


def _remember_visit(resp, property_id):
    from app import RECENT_COOKIE, RECENT_MAX
    prior = [i for i in request.cookies.get(RECENT_COOKIE, "").split(",") if i and i != property_id]
    resp.set_cookie(RECENT_COOKIE, ",".join([property_id, *prior][:RECENT_MAX]), max_age=60 * 60 * 24 * 90)


@bp.route("/properties/<property_id>")
def detail(property_id):
    conn = db.get_conn()
    prop, is_overhead, ctx = _load(conn, property_id)
    if not prop:
        flash(f"We couldn't find a property called '{property_id}'. Pick one from the Properties list.", "error")
        return redirect(url_for("properties.index"))

    start, end = _range(ctx)
    if is_overhead:
        cur_cost = kpis.costs(conn, property_id, start, end)
        cmp_b = compare_bounds(ctx)
        prev_cost = kpis.costs(conn, property_id, *cmp_b) if cmp_b else None
        ly_cost = kpis.costs(conn, property_id, *kpis.range_bounds(*kpis.same_period_last_year(
            ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])))
        tiles = [{"label": "Total costs", "value": f"£{cur_cost:,.0f}",
                  "delta": pct_delta(cur_cost, prev_cost) if prev_cost is not None else None,
                  "delta_ly": pct_delta(cur_cost, ly_cost)}] if cur_cost or prev_cost or ly_cost else []
        primary_tiles, secondary_tiles = tiles, []
    else:
        # Same primary/secondary grouping as Portfolio Overview -- reused
        # directly rather than re-implemented, so a property workspace's
        # Overview never drifts from the portfolio one's hierarchy.
        from routes.overview import kpi_rows
        primary_tiles, secondary_tiles, cur = kpi_rows(conn, property_id, ctx)
        if not (cur["revenue"] or cur["costs"] or cur["booked_nights"]):
            primary_tiles, secondary_tiles = [], []

    # Anchored + clipped to trailing 12 months -- see the matching note in
    # routes/overview.py on why a barely-started current month or years of
    # unclipped history both make the trend chart misleading.
    anchor_ym = f"{ctx['end_year']}-{ctx['end_month']:02d}"
    series = [s for s in kpis.adjusted_monthly_series(conn, property_id) if s["ym"] <= anchor_ym][-12:]
    yoy = adjusted_yoy_pairs(conn, property_id, (ctx["end_year"], ctx["end_month"]))

    resp = make_response(render_template(
        "property/overview.html", all_properties=get_properties(conn),
        **_ws(ctx, prop, "overview", is_overhead=is_overhead, primary_tiles=primary_tiles, secondary_tiles=secondary_tiles, yoy=yoy),
        months_json=json.dumps([s["ym"] for s in series]),
        income_json=json.dumps([s["revenue"] for s in series]),
        profit_json=json.dumps([s["net_profit"] for s in series]),
        costs_json=json.dumps([s["costs"] for s in series]),
        margin_json=json.dumps([round(s["margin"] * 100, 1) for s in series]),
        occupancy_json=json.dumps([round(s["occupancy"] * 100, 1) for s in series]),
    ))
    if not is_overhead:
        _remember_visit(resp, property_id)
    return resp


@bp.route("/properties/<property_id>/bookings")
def bookings(property_id):
    conn = db.get_conn()
    prop, is_overhead, ctx = _load(conn, property_id)
    if not prop:
        flash(f"We couldn't find a property called '{property_id}'. Pick one from the Properties list.", "error")
        return redirect(url_for("properties.index"))
    if is_overhead:
        return redirect(cx_url("properties.detail", property_id=property_id))

    start, end = _range(ctx)
    rows = conn.execute(
        """SELECT *, CAST(julianday(check_out) - julianday(check_in) AS INTEGER) AS nights
           FROM bookings WHERE property_id=? AND status='confirmed' AND reservation_id != 'monthly-aggregate'
             AND check_in < ? AND check_out > ? ORDER BY check_in DESC LIMIT 100""",
        (property_id, end, start),
    ).fetchall()

    resp = make_response(render_template(
        "property/bookings.html", all_properties=get_properties(conn),
        **_ws(ctx, prop, "bookings", is_overhead=is_overhead),
        booked_nights=kpis.booked_nights(conn, property_id, start, end),
        adr=kpis.adr(conn, property_id, start, end), bookings=rows,
    ))
    _remember_visit(resp, property_id)
    return resp


@bp.route("/properties/<property_id>/calendar")
def calendar_tab(property_id):
    """This flat's own booking calendar -- the same grid/query logic as
    the portfolio Bookings > Calendar tab (services.bookings.calendar_data),
    just scoped to this one property and never asking you to re-pick it."""
    conn = db.get_conn()
    prop, is_overhead, ctx = _load(conn, property_id)
    if not prop:
        flash(f"We couldn't find a property called '{property_id}'. Pick one from the Properties list.", "error")
        return redirect(url_for("properties.index"))
    if is_overhead:
        return redirect(cx_url("properties.detail", property_id=property_id))

    from routes.bookings import calendar_data
    data = calendar_data(conn, ctx, property_id)

    def month_href(y, m):
        return url_for("properties.calendar_tab", property_id=property_id,
                        **range_params(ctx, **{"from": f"{y}-{m:02d}-01", "to": f"{y}-{m:02d}-01"}))

    resp = make_response(render_template(
        "property/calendar.html", all_properties=get_properties(conn),
        **_ws(ctx, prop, "calendar", is_overhead=is_overhead),
        prev_href=month_href(data["py"], data["pm"]), next_href=month_href(data["ny"], data["nm"]),
        **{k: v for k, v in data.items() if k not in ("py", "pm", "ny", "nm")},
    ))
    _remember_visit(resp, property_id)
    return resp


@bp.route("/properties/<property_id>/performance")
def performance_tab(property_id):
    """How this one flat has been performing -- occupancy/ADR trend and
    booked nights for its own history, not a portfolio comparison view
    (that's what Bookings > Performance is for)."""
    conn = db.get_conn()
    prop, is_overhead, ctx = _load(conn, property_id)
    if not prop:
        flash(f"We couldn't find a property called '{property_id}'. Pick one from the Properties list.", "error")
        return redirect(url_for("properties.index"))
    if is_overhead:
        return redirect(cx_url("properties.detail", property_id=property_id))

    start, end = _range(ctx)
    cmp_b = compare_bounds(ctx)
    cur = kpis.adjusted_kpi_snapshot(conn, property_id, start, end)
    prev = kpis.adjusted_kpi_snapshot(conn, property_id, *cmp_b) if cmp_b else None

    def t(label, key, fmt, base):
        cv = cur[key]
        return {"label": label, "value": fmt(cv), "delta": pct_delta(cv, prev[key], min_base=base) if prev else None}
    tiles = [
        t("Occupancy", "occupancy", lambda v: f"{v * 100:.0f}%", 0.05),
        t("ADR", "adr", lambda v: f"£{v:,.0f}", 20),
        t("Booked nights", "booked_nights", lambda v: f"{v:,.0f}", 2),
        t("RevPAR", "revpar", lambda v: f"£{v:,.0f}", 20),
    ] if (cur["booked_nights"] or cur["occupancy"]) else []

    # Same anchoring rule as every other trend chart here: this property's
    # own recorded months, clipped to the trailing 12 up to the selected
    # period, so a barely-started month or years of history never mislead.
    anchor_ym = f"{ctx['end_year']}-{ctx['end_month']:02d}"
    series = [s for s in kpis.adjusted_monthly_series(conn, property_id) if s["ym"] <= anchor_ym][-12:]
    portfolio_by_ym = {s["ym"]: s for s in kpis.adjusted_monthly_series(conn, None)}

    months = [s["ym"] for s in series]
    occ = [round(s["occupancy"] * 100, 1) for s in series]
    occ_portfolio = [round(portfolio_by_ym[ym]["occupancy"] * 100, 1) if ym in portfolio_by_ym else None for ym in months]
    adr_series = [round(s["adr"], 0) if s["adr"] else None for s in series]

    resp = make_response(render_template(
        "property/performance.html", all_properties=get_properties(conn),
        **_ws(ctx, prop, "performance", is_overhead=is_overhead, tiles=tiles),
        months_json=json.dumps(months), occ_json=json.dumps(occ), occ_portfolio_json=json.dumps(occ_portfolio),
        adr_json=json.dumps(adr_series),
    ))
    _remember_visit(resp, property_id)
    return resp


@bp.route("/properties/<property_id>/expenses")
def expenses_tab(property_id):
    conn = db.get_conn()
    prop, is_overhead, ctx = _load(conn, property_id)
    if not prop:
        flash(f"We couldn't find a property called '{property_id}'. Pick one from the Properties list.", "error")
        return redirect(url_for("properties.index"))

    start, end = _range(ctx)
    transactions = conn.execute(
        """SELECT * FROM transactions WHERE property_id=? AND direction='expense' AND date>=? AND date<?
           ORDER BY date DESC, id DESC LIMIT 200""",
        (property_id, start, end),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM transactions WHERE property_id=? AND direction='expense' AND date>=? AND date<?",
        (property_id, start, end)).fetchone()

    resp = make_response(render_template(
        "property/expenses.html", all_properties=get_properties(conn),
        **_ws(ctx, prop, "expenses", is_overhead=is_overhead),
        expenses=transactions, ledger_total=total,
    ))
    if not is_overhead:
        _remember_visit(resp, property_id)
    return resp


@bp.route("/properties/<property_id>/documents")
def documents_tab(property_id):
    conn = db.get_conn()
    prop, is_overhead, ctx = _load(conn, property_id)
    if not prop:
        flash(f"We couldn't find a property called '{property_id}'. Pick one from the Properties list.", "error")
        return redirect(url_for("properties.index"))

    documents = conn.execute(
        "SELECT * FROM documents WHERE property_id=? ORDER BY uploaded_at DESC", (property_id,)
    ).fetchall()
    start, end = _range(ctx)
    health = None if is_overhead else health_for(conn, property_id, start, end)

    resp = make_response(render_template(
        "property/documents.html", all_properties=get_properties(conn),
        **_ws(ctx, prop, "documents", is_overhead=is_overhead),
        documents=documents, extraction_available=extraction.available(),
        health=health, health_period=ctx["display"], health_title_text=health_title(ctx),
        prefill_type=request.args.get("type", ""),
    ))
    if not is_overhead:
        _remember_visit(resp, property_id)
    return resp


@bp.route("/properties/<property_id>/health")
def health_drawer(property_id):
    """The "what exactly is missing?" drawer: each expected source for the
    selected period, received or missing, with an Upload beside each gap."""
    conn = db.get_conn()
    prop, is_overhead, ctx = _load(conn, property_id)
    if not prop or is_overhead:
        return "<p class='note'>No data requirements for this property.</p>", 404
    start, end = _range(ctx)
    health = health_for(conn, property_id, start, end)
    return render_template("partials/health_drawer.html", prop=prop, health=health, health_period=ctx["display"],
                           health_title=health_title(ctx))


@bp.route("/properties/<property_id>/settings")
def settings_tab(property_id):
    conn = db.get_conn()
    prop, is_overhead, ctx = _load(conn, property_id)
    if not prop:
        flash(f"We couldn't find a property called '{property_id}'. Pick one from the Properties list.", "error")
        return redirect(url_for("properties.index"))

    start, end = _range(ctx)
    resp = make_response(render_template(
        "property/settings.html", all_properties=get_properties(conn),
        **_ws(ctx, prop, "settings", is_overhead=is_overhead),
        checklist=_checklist(conn, property_id, is_overhead, ctx["end_year"], ctx["end_month"]),
        fee_pct=prop["management_fee_pct"], your_income=None if is_overhead else kpis.business_income(conn, property_id, start, end),
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
        flash("Enter a name for the new property so we can add it.", "error")
        return redirect(url_for("overview.index"))
    slug = db.unique_slug(conn, db.slugify(name))
    conn.execute("INSERT INTO properties (id, code, name, address, type) VALUES (?,?,?,?,'flat')",
                 (slug, slug.upper()[:10], name, address))
    seed_defaults(conn, slug)
    conn.commit()
    flash(f"\u2713 Added {name}. Upload its first document or add an expense to start building its figures.", "success")
    return redirect(url_for("properties.detail", property_id=slug))


@bp.route("/properties/<property_id>/ownership", methods=["POST"])
def save_ownership(property_id):
    conn = db.get_conn()
    prop = get_property(conn, property_id)
    if not prop or prop["type"] == "overhead":
        flash("We couldn't find that property. Pick one from the Properties list.", "error")
        return redirect(url_for("properties.index"))
    kind = request.form.get("kind", "owned")
    fee = None
    if kind == "managed":
        raw = (request.form.get("fee") or "").strip()
        try:
            fee = max(0.0, min(100.0, float(raw)))
        except ValueError:
            flash("Enter the management fee as a percentage, for example 15.", "error")
            return redirect(url_for("properties.settings_tab", property_id=property_id))
    conn.execute("UPDATE properties SET management_fee_pct=? WHERE id=?", (fee, property_id))
    conn.commit()
    if fee:
        flash(f"\u2713 {prop['name']} is set as managed -- this business earns {fee:g}% of its revenue.", "success")
    else:
        flash(f"\u2713 {prop['name']} is set as fully owned -- this business earns its full net profit.", "success")
    return redirect(url_for("properties.settings_tab", property_id=property_id))


@bp.route("/property/<property_id>/sync_calendar", methods=["POST"])
def sync_calendar(property_id):
    conn = db.get_conn()
    prop = get_property(conn, property_id)
    if not prop:
        flash(f"We couldn't find a property called '{property_id}'. Pick one from the Properties list.", "error")
        return redirect(url_for("overview.index"))

    url = (request.form.get("ical_url") or "").strip() or prop["ical_url"]
    if not url:
        flash("Paste the calendar's export link first (Airbnb: listing → Availability → Export calendar), then sync.", "warning")
        return redirect(url_for("properties.settings_tab", property_id=property_id))

    try:
        ics_text = ical_sync.fetch(url)
        events = ical_sync.parse_events(ics_text)
    except ValueError as e:
        flash(f"We couldn't read that calendar: {e} Check the link is the calendar's export URL and try again.", "error")
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
    flash(f"\u2713 Calendar synced: {added} new reservation{'s' if added != 1 else ''} added" + (f", {skipped} already on file." if skipped else "."), "success")
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
        flash("Enter the amount as a number, for example 42.50.", "error")
        return redirect(url_for("properties.expenses_tab", property_id=property_id))
    category = request.form.get("category", "purchase")
    direction = "income" if category == "booking_income" else "expense"
    vendor_name = request.form.get("vendor", "")
    vendor_id = get_or_create_vendor(conn, vendor_name)
    conn.execute(
        """INSERT INTO transactions (property_id, date, vendor, vendor_id, description, amount, direction, category, source)
           VALUES (?,?,?,?,?,?,?,?,'manual')""",
        (property_id, f"{year}-{month:02d}-01", vendor_name, vendor_id,
         request.form.get("description", ""), amount, direction, category),
    )
    conn.commit()
    flash("\u2713 Expense added.", "success")
    return redirect(url_for("properties.expenses_tab", property_id=property_id))
