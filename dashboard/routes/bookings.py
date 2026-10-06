import calendar as cal
import datetime
import json
import re

from flask import Blueprint, redirect, render_template, request, url_for

import db
import services.ingest as ingest
import services.kpis as kpis
import services.sources as src
from services.common import METRIC_INFO, MONTH_ABBR, MONTH_NAMES, channel_key, get_properties, pct_delta
from services.context import compare_bounds, range_params, request_context

bp = Blueprint("bookings", __name__)


def _has_real_bookings(conn):
    """Whether ANY property has a real day-level booking (not an Excel
    monthly-aggregate row) -- same signal as properties._ws()'s
    has_calendar_data, used here to hide the portfolio-wide Calendar tab
    the same way, for the same reason."""
    return bool(conn.execute(
        "SELECT 1 FROM bookings WHERE status='confirmed' AND reservation_id != 'monthly-aggregate' LIMIT 1").fetchone())


@bp.route("/occupancy")
def legacy_occupancy():
    return redirect(url_for("bookings.performance"), code=301)


@bp.route("/bookings")
def index():
    conn = db.get_conn()
    ctx = request_context(conn)
    pid = ctx["property_id"]
    all_props = get_properties(conn)
    viewing = next((p for p in all_props if p["id"] == pid), None) if pid else None
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    prev = compare_bounds(ctx)
    label = ctx["compare_display"] or ""
    # Same MTD-vs-full-prior-month fix as Overview's tiles: don't show a
    # coloured delta when "This Month" (partial so far) is being weighed
    # against a full prior month -- it isn't a real decline.
    mtd = ctx["partial"] and ctx["choice"] == "this_month"

    def metric(fn, *a):
        cur = fn(conn, pid, start, end)
        return cur, (fn(conn, pid, *prev) if prev else None)

    reservations, prev_res = metric(kpis.reservation_count)
    nights, prev_nights = metric(kpis.booked_nights)
    revenue, prev_rev = metric(kpis.accommodation_revenue)
    tiles = [
        {"label": "Reservations", "value": f"{reservations:,}", "delta": None if mtd else (pct_delta(reservations, prev_res, min_base=2) if prev else None)},
        {"label": "Booked nights", "value": f"{nights:,}", "delta": None if mtd else (pct_delta(nights, prev_nights, min_base=5) if prev else None)},
        {"label": "ADR", "value": f"£{kpis.adr(conn, pid, start, end):,.0f}", "info": METRIC_INFO["adr"], "delta": None},
        {"label": "Avg stay", "value": (f"{kpis.avg_stay(conn, pid, start, end):.1f} nights" if kpis.avg_stay(conn, pid, start, end) else "—"), "delta": None},
        {"label": "Gross Booking Revenue", "value": f"£{revenue:,.0f}", "info": METRIC_INFO["gross_booking_revenue"], "delta": None if mtd else (pct_delta(revenue, prev_rev, min_base=100) if prev else None)},
    ]

    today = datetime.date.today()
    window_end = today + datetime.timedelta(days=30)
    upcoming = {
        "occupancy": kpis.occupancy(conn, pid, today.isoformat(), window_end.isoformat()),
        "revenue": kpis.accommodation_revenue(conn, pid, today.isoformat(), window_end.isoformat()),
        "reservations": kpis.reservation_count(conn, pid, today.isoformat(), window_end.isoformat()),
    }

    # Channel mix follows the same rule as every KPI: only reservations in months whose active source is the detailed bookings.
    scope, sparams = ("AND property_id=?", (pid,)) if pid else ("", ())
    S = src.Sources(conn, pid, start, end)
    mix = {}
    for r in conn.execute(
            f"""SELECT property_id, COALESCE(NULLIF(platform,''),'other') platform, net_revenue, check_in FROM bookings
                WHERE status='confirmed' AND reservation_id != 'monthly-aggregate' AND check_in>=? AND check_in<? {scope}""",
            (start, end, *sparams)):
        if S.active(r["property_id"], r["check_in"][:7]) != src.DETAILED:
            continue
        m = mix.setdefault(r["platform"], {"platform": r["platform"], "n": 0, "amt": 0.0})
        m["n"] += 1
        m["amt"] += r["net_revenue"] or 0.0
    channel_rows = sorted(mix.values(), key=lambda m: -m["amt"])

    return render_template(
        "bookings/overview.html", active="bookings", active_bookings_tab="overview",
        all_properties=all_props, active_property=None, context_bar=True, ctx=ctx, viewing=viewing,
        current_month=ctx["display"], compare_label=label, tiles=tiles, upcoming=upcoming, channel_rows=channel_rows,
        has_calendar_data=_has_real_bookings(conn),
    )


@bp.route("/bookings/<int:booking_id>/drawer")
def booking_drawer(booking_id):
    conn = db.get_conn()
    b = conn.execute(
        """SELECT b.*, p.name AS property_name,
                  CAST(julianday(b.check_out) - julianday(b.check_in) AS INTEGER) AS nights
           FROM bookings b JOIN properties p ON p.id = b.property_id WHERE b.id=?""", (booking_id,)).fetchone()
    if not b:
        return "<p class='note'>Booking not found.</p>", 404
    line = None
    if b["document_id"]:
        # the extracted line this reservation came from: same confirmation code, else same stay dates
        line = (conn.execute("SELECT * FROM document_items WHERE document_id=? AND reservation_id=? AND reservation_id != '' LIMIT 1",
                             (b["document_id"], b["reservation_id"])).fetchone()
                or conn.execute("SELECT * FROM document_items WHERE document_id=? AND check_in=? AND check_out=? LIMIT 1",
                                (b["document_id"], b["check_in"], b["check_out"])).fetchone())
    return render_template("partials/booking_drawer.html", b=b, prov=ingest.provenance(conn, b["document_id"], line))


@bp.route("/bookings/day/<day>")
def day_drawer(day):
    conn = db.get_conn()
    try:
        datetime.date.fromisoformat(day)
    except ValueError:
        return "<p class='note'>Unknown date.</p>", 404
    rows = conn.execute(
        """SELECT b.*, p.name AS property_name,
                  CAST(julianday(b.check_out) - julianday(b.check_in) AS INTEGER) AS nights
           FROM bookings b JOIN properties p ON p.id = b.property_id
           WHERE b.status='confirmed' AND b.reservation_id != 'monthly-aggregate' AND b.check_in <= ? AND b.check_out > ?
           ORDER BY p.name""", (day, day)).fetchall()
    total = len(get_properties(conn, include_overhead=False))
    d = datetime.date.fromisoformat(day)
    return render_template("partials/day_drawer.html", rows=rows, total=total, label=f"{d.day} {MONTH_NAMES[d.month]} {d.year}")


def calendar_data(conn, ctx, pid):
    """The month grid + upcoming reservations for a given context, scoped
    to one property (pid set) or the whole portfolio (pid=None) -- shared
    by the portfolio Bookings > Calendar tab and a property workspace's
    own Calendar tab, so the same query logic backs both."""
    year, month = ctx["end_year"], ctx["end_month"]
    start, end = kpis.month_bounds(year, month)
    total_flats = 1 if pid else (len(get_properties(conn, include_overhead=False)) or 1)
    scope, sparams = ("AND b.property_id=?", (pid,)) if pid else ("", ())

    overlapping = conn.execute(
        f"""SELECT b.check_in, b.check_out, b.platform FROM bookings b WHERE b.status='confirmed' AND b.reservation_id != 'monthly-aggregate'
             AND b.check_in<? AND b.check_out>? {scope}""",
        (end, start, *sparams),
    ).fetchall()
    spans = [(datetime.date.fromisoformat(r["check_in"]), datetime.date.fromisoformat(r["check_out"]), channel_key(r["platform"])) for r in overlapping]

    days_in_month = cal.monthrange(year, month)[1]
    first_weekday = datetime.date(year, month, 1).weekday()  # Monday=0
    day_cells = [None] * first_weekday
    for d in range(1, days_in_month + 1):
        day = datetime.date(year, month, d)
        on_day = [ch for ci, co, ch in spans if ci <= day < co]
        occupied = len(on_day)
        channels = [k for k in ("airbnb", "booking", "direct", "vrbo", "other") if k in on_day]
        day_cells.append({"day": d, "iso": day.isoformat(), "occupied": occupied, "total": total_flats, "channels": channels,
                           "pct": round(occupied / total_flats * 100)})

    py, pm = kpis.prior_month(year, month)
    ny, nm = kpis.add_months(year, month, 1)

    upcoming_rows = conn.execute(
        f"""SELECT b.*, p.name AS property_name,
                  CAST(julianday(b.check_out) - julianday(b.check_in) AS INTEGER) AS nights
           FROM bookings b JOIN properties p ON p.id = b.property_id
           WHERE b.status='confirmed' AND b.reservation_id != 'monthly-aggregate' AND b.check_in >= ? {scope}
           ORDER BY b.check_in ASC LIMIT 30""",
        (datetime.date.today().isoformat(), *sparams),
    ).fetchall()

    return {
        "month_label": f"{MONTH_NAMES[month]} {year}", "day_cells": day_cells,
        "py": py, "pm": pm, "ny": ny, "nm": nm,
        "weekday_labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "upcoming_rows": upcoming_rows,
    }


@bp.route("/bookings/calendar")
def calendar_tab(property_id=None):
    conn = db.get_conn()
    legacy = request.args.get("month", "")
    if re.match(r"^\d{4}-\d{2}$", legacy):  # older ?month=YYYY-MM links
        return redirect(url_for("bookings.calendar_tab", **{"from": legacy + "-01", "to": legacy + "-01"}))
    ctx = request_context(conn)
    pid = ctx["property_id"]
    all_props = get_properties(conn)
    viewing = next((p for p in all_props if p["id"] == pid), None) if pid else None
    data = calendar_data(conn, ctx, pid)

    # month arrows move the shared context, so every other page follows
    def month_href(y, m):
        return url_for("bookings.calendar_tab", **range_params(ctx, **{"from": f"{y}-{m:02d}-01", "to": f"{y}-{m:02d}-01"}))

    return render_template(
        "bookings/calendar.html", active="bookings", active_bookings_tab="calendar",
        all_properties=all_props, active_property=None, context_bar=True, ctx=ctx, viewing=viewing, hide_compare=True,
        prev_href=month_href(data["py"], data["pm"]), next_href=month_href(data["ny"], data["nm"]),
        has_calendar_data=_has_real_bookings(conn),
        **{k: v for k, v in data.items() if k not in ("py", "pm", "ny", "nm")},
    )


@bp.route("/bookings/performance")
def performance(property_id=None):
    conn = db.get_conn()
    ctx = request_context(conn)
    # This page has no property selector (hide_property=True below) and
    # always shows every flat -- a stray "property" left over from
    # browsing elsewhere shouldn't make "Reset to latest" appear here.
    ctx["is_latest"] = ctx["period_is_latest"] and ctx["compare"] == "previous_period"
    flats = get_properties(conn, include_overhead=False)
    year, month = ctx["end_year"], ctx["end_month"]
    py, pm = kpis.prior_month(year, month)
    mtd = ctx["partial"] and ctx["choice"] == "this_month"
    # Anchored at the selected period and clipped to trailing 12 months --
    # otherwise a barely-started current month (or years of history) would
    # either fake a cliff at the end of the trend line or make it
    # unreadable. See the matching note in routes/overview.py.
    anchor_ym = f"{year}-{month:02d}"
    months = [m for m in kpis.months_with_data(conn, None) if m <= anchor_ym][-12:]

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
            "delta": None if mtd else (pct_delta(cur_occ, prev_occ) if f"{py}-{pm:02d}" in own_months else None),
            "adr": kpis.adr(conn, p["id"], cstart, cend),
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

    heatmap_months = months[-12:]
    heatmap_rows = [{"id": r["id"], "name": r["name"], "cells": series_by_property[r["name"]][-12:]} for r in rows]

    return render_template(
        "bookings/performance.html", active="bookings", active_bookings_tab="performance",
        all_properties=get_properties(conn), active_property=None, context_bar=True, ctx=ctx, hide_property=True,
        current_month=f"{MONTH_NAMES[month]} {year}", rows=rows,
        heatmap_months=[MONTH_ABBR[int(ym.split('-')[1])] + " " + ym.split('-')[0][2:] for ym in heatmap_months],
        heatmap_rows=heatmap_rows,
        months_json=json.dumps(months), portfolio_json=json.dumps(portfolio_series),
        has_calendar_data=_has_real_bookings(conn),
    )
