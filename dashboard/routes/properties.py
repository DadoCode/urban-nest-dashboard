import datetime
import json

from flask import Blueprint, flash, make_response, redirect, render_template, request, url_for

import db
import services.extraction as extraction
import services.ical_sync as ical_sync
import services.kpis as kpis
from services.common import METRIC_INFO, MONTH_NAMES, adjusted_yoy_pairs, get_properties, get_property, pct_delta
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


def _ws(conn, ctx, prop, tab, **extra):
    """Template variables every workspace tab shares."""
    # Whether the Calendar tab has anything real to show -- day-level
    # bookings, not the monthly-aggregate rows an Excel import creates.
    # Not the same signal as _checklist()'s ical-only has_calendar: a
    # property can have real day-level bookings from an uploaded
    # statement with no iCal ever connected, and gating on iCal alone
    # would wrongly hide a tab that actually has data. Without this, the
    # tab showed an empty grid every time for any property that had
    # never synced a calendar, even ones with real booking data.
    has_calendar_data = bool(conn.execute(
        "SELECT 1 FROM bookings WHERE property_id=? AND status='confirmed' AND reservation_id != 'monthly-aggregate' LIMIT 1",
        (prop["id"],)).fetchone())
    return {"active": "properties", "active_property": prop["id"], "active_tab": tab, "prop": prop,
            "context_bar": True, "ctx": ctx, "fixed_property": prop, "hide_property": True,
            "has_calendar_data": has_calendar_data,
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


def _overview_tiles(conn, prop, ctx):
    """Overview's tile set genuinely differs by business model -- not a
    redesign for its own sake. For an operated flat, Revenue and
    Property Profit are meaningfully different numbers. For a managed
    flat, adjusted Revenue and Property Profit are the SAME figure
    (both are just the fee) -- showing both under those two names on
    the property's own Overview is exactly the "why is Revenue the same
    as Profit" confusion flagged earlier. A managed property instead
    gets Gross Booking Revenue (the real guest booking value) next to
    Management Fee Earned (what Urban Nest actually keeps): two
    genuinely different numbers, each labelled for what it actually
    is -- matching Phase 4's item 9 exactly."""
    from routes.expenses import _costs
    property_id = prop["id"]
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    cmp_b = compare_bounds(ctx)
    mtd = ctx["partial"] and ctx["choice"] == "this_month"
    period_label = ("MTD, " if mtd else "") + ctx["display"]

    def d(cur_v, prev_v, base=0):
        return None if mtd else (pct_delta(cur_v, prev_v, min_base=base) if prev_v is not None else None)

    if prop["management_fee_pct"]:
        cur_gross = kpis.revenue(conn, property_id, start, end)
        cur_fee = kpis.business_income(conn, property_id, start, end)
        cur_occ = kpis.occupancy(conn, property_id, start, end)
        prev_gross, prev_fee, prev_occ = (kpis.revenue(conn, property_id, *cmp_b), kpis.business_income(conn, property_id, *cmp_b),
                                           kpis.occupancy(conn, property_id, *cmp_b)) if cmp_b else (None, None, None)
        primary = [
            {"key": "gross_booking_revenue", "label": f"Gross Booking Revenue — {period_label}", "value": f"£{cur_gross:,.0f}",
             "delta": d(cur_gross, prev_gross, 100), "info": METRIC_INFO.get("gross_booking_revenue")},
            {"key": "fee", "label": "Management Fee Earned", "value": f"£{cur_fee:,.0f}",
             "delta": d(cur_fee, prev_fee, 20), "info": METRIC_INFO.get("fee")},
            {"key": "occupancy", "label": "Occupancy", "value": f"{cur_occ * 100:.0f}%", "delta": d(cur_occ, prev_occ, 0.05)},
        ]
        has_data = bool(cur_gross or cur_occ or cur_fee)
        secondary = [
            {"label": "ADR", "value": f"£{kpis.adr(conn, property_id, start, end):,.0f}", "info": METRIC_INFO.get("adr")},
            {"label": "RevPAR", "value": f"£{kpis.revpar(conn, property_id, start, end):,.0f}", "info": METRIC_INFO.get("revpar")},
        ] if has_data else []
    else:
        snap = kpis.adjusted_kpi_snapshot(conn, property_id, start, end)
        prev_snap = kpis.adjusted_kpi_snapshot(conn, property_id, *cmp_b) if cmp_b else None
        cur_costs = _costs(conn, start, end, property_id=property_id)
        prev_costs = _costs(conn, *cmp_b, property_id=property_id) if cmp_b else None
        primary = [
            {"key": "revenue", "label": f"Urban Nest Revenue — {period_label}", "value": f"£{snap['revenue']:,.0f}",
             "delta": d(snap["revenue"], prev_snap["revenue"] if prev_snap else None, 100), "info": METRIC_INFO.get("revenue")},
            {"key": "property_costs", "label": "Property Costs", "value": f"£{cur_costs:,.0f}",
             "delta": d(cur_costs, prev_costs, 100), "info": METRIC_INFO.get("property_costs")},
            {"key": "net_profit", "label": "Property Profit", "value": f"£{snap['net_profit']:,.0f}",
             "delta": d(snap["net_profit"], prev_snap["net_profit"] if prev_snap else None, 1000), "info": METRIC_INFO.get("net_profit")},
            {"key": "occupancy", "label": "Occupancy", "value": f"{snap['occupancy'] * 100:.0f}%",
             "delta": d(snap["occupancy"], prev_snap["occupancy"] if prev_snap else None, 0.05)},
        ]
        has_data = bool(snap["revenue"] or snap["occupancy"] or cur_costs)
        secondary = [
            {"label": "ADR", "value": f"£{snap['adr']:,.0f}", "info": METRIC_INFO.get("adr")},
            {"label": "RevPAR", "value": f"£{snap['revpar']:,.0f}", "info": METRIC_INFO.get("revpar")},
        ] if has_data else []
    return (primary, secondary) if has_data else ([], [])


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
    # Same MTD-vs-full-prior-period fix as Overview's tiles (see
    # routes/overview.py's kpi_rows()): a partial "This Month" compared
    # against a full prior month/year isn't a real decline.
    mtd = ctx["partial"] and ctx["choice"] == "this_month"
    # Overview is the current-period result only -- Phase 4 moves every
    # historical trend chart (and Year on year) to Performance, which
    # owns "how is this changing over time". The overhead cost-centre
    # has no Performance tab of its own (see _shell.html), so its one
    # chart stays here; a real flat's chart data is no longer prepared
    # on this route at all.
    if is_overhead:
        cur_cost = kpis.costs(conn, property_id, start, end)
        cmp_b = compare_bounds(ctx)
        prev_cost = kpis.costs(conn, property_id, *cmp_b) if cmp_b else None
        ly_cost = kpis.costs(conn, property_id, *kpis.range_bounds(*kpis.same_period_last_year(
            ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])))
        primary_tiles = [{"label": "Total costs", "value": f"£{cur_cost:,.0f}",
                  "delta": None if mtd else (pct_delta(cur_cost, prev_cost) if prev_cost is not None else None),
                  "delta_ly": None if mtd else pct_delta(cur_cost, ly_cost)}] if cur_cost or prev_cost or ly_cost else []
        secondary_tiles = []
        anchor_ym = f"{ctx['end_year']}-{ctx['end_month']:02d}"
        series = [s for s in kpis.monthly_series(conn, property_id) if s["ym"] <= anchor_ym][-12:]
        chart_kwargs = {"months_json": json.dumps([s["ym"] for s in series]),
                         "costs_json": json.dumps([s["costs"] for s in series])}
    else:
        primary_tiles, secondary_tiles = _overview_tiles(conn, prop, ctx)
        chart_kwargs = {}

    resp = make_response(render_template(
        "property/overview.html", all_properties=get_properties(conn),
        **_ws(conn, ctx, prop, "overview", is_overhead=is_overhead, primary_tiles=primary_tiles, secondary_tiles=secondary_tiles),
        **chart_kwargs,
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
        **_ws(conn, ctx, prop, "bookings", is_overhead=is_overhead),
        booked_nights=kpis.booked_nights(conn, property_id, start, end),
        adr=kpis.adr(conn, property_id, start, end), bookings=rows,
        # Bookings is the reservation evidence layer -- what guests
        # actually booked and paid, so it's Gross Booking Revenue (the
        # true guest value) here, never the adjusted Urban Nest figure
        # Overview/Performance show.
        gross_booking_revenue=kpis.accommodation_revenue(conn, property_id, start, end),
        avg_stay=kpis.avg_stay(conn, property_id, start, end),
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
        **_ws(conn, ctx, prop, "calendar", is_overhead=is_overhead),
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
    managed = bool(prop["management_fee_pct"])

    mtd = ctx["partial"] and ctx["choice"] == "this_month"

    def t(label, key, fmt, base):
        cv = cur[key]
        return {"label": label, "value": fmt(cv), "info": METRIC_INFO.get(key),
                "delta": None if mtd else (pct_delta(cv, prev[key], min_base=base) if prev else None)}
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
    revpar_series = [round(s["revpar"], 0) if s["revpar"] else None for s in series]

    # Financial Performance -- moved here from Overview (Phase 4: Overview
    # is current-period only, Performance owns "how is this changing over
    # time"). The series themselves differ by model, same reasoning as
    # _overview_tiles(): a managed flat's adjusted revenue/costs/profit
    # would just be the fee trend plotted three times over, so it gets its
    # own genuinely different pair of series instead (gross booking value
    # vs the fee Urban Nest actually keeps from it).
    if managed:
        gross_series = [round(kpis.revenue(conn, property_id, *kpis.month_bounds(*map(int, s["ym"].split("-")))), 2) for s in series]
        fee_series = [round(s["net_profit"], 2) for s in series]
        financial = {"months": months, "series_a": gross_series, "series_a_label": "Gross Booking Revenue",
                     "series_b": fee_series, "series_b_label": "Management Fee Earned", "costs": None}
    else:
        financial = {"months": months, "series_a": [round(s["revenue"], 2) for s in series], "series_a_label": "Urban Nest Revenue",
                     "series_b": [round(s["net_profit"], 2) for s in series], "series_b_label": "Property Profit",
                     "costs": [round(s["costs"], 2) for s in series]}

    yoy = adjusted_yoy_pairs(conn, property_id, (ctx["end_year"], ctx["end_month"]))

    resp = make_response(render_template(
        "property/performance.html", all_properties=get_properties(conn),
        **_ws(conn, ctx, prop, "performance", is_overhead=is_overhead, tiles=tiles, yoy=yoy, managed=managed),
        months_json=json.dumps(months), occ_json=json.dumps(occ), occ_portfolio_json=json.dumps(occ_portfolio),
        adr_json=json.dumps(adr_series), revpar_json=json.dumps(revpar_series),
        financial_json=json.dumps(financial),
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
    # Same Phase 3 semantics as the portfolio Expenses page: a
    # management-fee transfer is Urban Nest's own income, not a cost --
    # excluded here too, so "what did this property cost" never
    # silently includes money Urban Nest earned from it.
    transactions = conn.execute(
        """SELECT * FROM transactions WHERE property_id=? AND direction='expense' AND category != 'management_fee'
           AND date>=? AND date<? ORDER BY date DESC, id DESC LIMIT 200""",
        (property_id, start, end),
    ).fetchall()
    total = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM transactions
           WHERE property_id=? AND direction='expense' AND category != 'management_fee' AND date>=? AND date<?""",
        (property_id, start, end)).fetchone()

    resp = make_response(render_template(
        "property/expenses.html", all_properties=get_properties(conn),
        **_ws(conn, ctx, prop, "expenses", is_overhead=is_overhead),
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
        **_ws(conn, ctx, prop, "documents", is_overhead=is_overhead),
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
        **_ws(conn, ctx, prop, "settings", is_overhead=is_overhead),
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
