import calendar as cal
import datetime
import json
import re

from flask import Blueprint, redirect, render_template, request, url_for

import db
import services.kpis as kpis
from services.common import MONTH_ABBR, MONTH_NAMES, get_properties, pct_delta

bp = Blueprint("bookings", __name__)


@bp.route("/occupancy")
def legacy_occupancy():
    return redirect(url_for("bookings.performance"), code=301)


@bp.route("/bookings")
def index():
    conn = db.get_conn()
    flats = get_properties(conn, include_overhead=False)
    year, month = kpis.current_period(conn)
    start, end = kpis.month_bounds(year, month)
    py, pm = kpis.prior_month(year, month)
    pstart, pend = kpis.month_bounds(py, pm)

    reservations = kpis.reservation_count(conn, None, start, end)
    prev_reservations = kpis.reservation_count(conn, None, pstart, pend)
    nights = kpis.booked_nights(conn, None, start, end)
    prev_nights = kpis.booked_nights(conn, None, pstart, pend)
    revenue = kpis.accommodation_revenue(conn, None, start, end)
    prev_revenue = kpis.accommodation_revenue(conn, None, pstart, pend)

    tiles = [
        {"label": f"Reservations — {MONTH_NAMES[month]} {year}", "value": f"{reservations:,}",
         "delta": pct_delta(reservations, prev_reservations, min_base=2)},
        {"label": "Booked nights", "value": f"{nights:,}",
         "delta": pct_delta(nights, prev_nights, min_base=5)},
        {"label": "ADR", "value": f"£{kpis.adr(conn, None, start, end):,.0f}", "delta": None},
        {"label": "Avg stay", "value": f"{kpis.avg_stay(conn, None, start, end):.1f} nights", "delta": None},
        {"label": "Confirmed revenue", "value": f"£{revenue:,.0f}",
         "delta": pct_delta(revenue, prev_revenue, min_base=100)},
    ]

    today = datetime.date.today()
    window_end = today + datetime.timedelta(days=30)
    upcoming = {
        "occupancy": kpis.occupancy(conn, None, today.isoformat(), window_end.isoformat()),
        "revenue": kpis.accommodation_revenue(conn, None, today.isoformat(), window_end.isoformat()),
        "reservations": kpis.reservation_count(conn, None, today.isoformat(), window_end.isoformat()),
    }

    channel_rows = conn.execute(
        """SELECT COALESCE(NULLIF(platform,''),'other') platform, COUNT(*) n, SUM(net_revenue) amt
           FROM bookings WHERE status='confirmed' AND reservation_id != 'monthly-aggregate'
             AND check_in>=? AND check_in<? GROUP BY platform ORDER BY amt DESC""",
        (start, end),
    ).fetchall()

    return render_template(
        "bookings/overview.html", active="bookings", active_bookings_tab="overview",
        all_properties=get_properties(conn), active_property=None,
        current_month=f"{MONTH_NAMES[month]} {year}", tiles=tiles, upcoming=upcoming, channel_rows=channel_rows,
    )


@bp.route("/bookings/calendar")
def calendar_tab(property_id=None):
    conn = db.get_conn()
    month_param = request.args.get("month", "")
    if re.match(r"^\d{4}-\d{2}$", month_param):
        year, month = map(int, month_param.split("-"))
    else:
        year, month = kpis.current_period(conn)
    start, end = kpis.month_bounds(year, month)
    total_flats = len(get_properties(conn, include_overhead=False)) or 1

    overlapping = conn.execute(
        """SELECT check_in, check_out FROM bookings WHERE status='confirmed' AND reservation_id != 'monthly-aggregate'
             AND check_in<? AND check_out>?""",
        (end, start),
    ).fetchall()
    spans = [(datetime.date.fromisoformat(r["check_in"]), datetime.date.fromisoformat(r["check_out"])) for r in overlapping]

    days_in_month = cal.monthrange(year, month)[1]
    first_weekday = datetime.date(year, month, 1).weekday()  # Monday=0
    day_cells = [None] * first_weekday
    for d in range(1, days_in_month + 1):
        day = datetime.date(year, month, d)
        occupied = sum(1 for ci, co in spans if ci <= day < co)
        day_cells.append({"day": d, "occupied": occupied, "total": total_flats,
                           "pct": round(occupied / total_flats * 100)})

    py, pm = kpis.prior_month(year, month)
    ny, nm = kpis.add_months(year, month, 1)

    upcoming_rows = conn.execute(
        """SELECT b.*, p.name AS property_name,
                  CAST(julianday(b.check_out) - julianday(b.check_in) AS INTEGER) AS nights
           FROM bookings b JOIN properties p ON p.id = b.property_id
           WHERE b.status='confirmed' AND b.reservation_id != 'monthly-aggregate' AND b.check_in >= ?
           ORDER BY b.check_in ASC LIMIT 30""",
        (datetime.date.today().isoformat(),),
    ).fetchall()

    return render_template(
        "bookings/calendar.html", active="bookings", active_bookings_tab="calendar",
        all_properties=get_properties(conn), active_property=None,
        month_label=f"{MONTH_NAMES[month]} {year}", day_cells=day_cells,
        prev_month=f"{py}-{pm:02d}", next_month=f"{ny}-{nm:02d}",
        weekday_labels=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        upcoming_rows=upcoming_rows,
    )


@bp.route("/bookings/performance")
def performance(property_id=None):
    conn = db.get_conn()
    flats = get_properties(conn, include_overhead=False)
    year, month = kpis.current_period(conn)
    py, pm = kpis.prior_month(year, month)
    # Anchored at the current period and clipped to trailing 12 months --
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
            "delta": pct_delta(cur_occ, prev_occ) if f"{py}-{pm:02d}" in own_months else None,
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
    heatmap_rows = [{"name": r["name"], "cells": series_by_property[r["name"]][-12:]} for r in rows]

    return render_template(
        "bookings/performance.html", active="bookings", active_bookings_tab="performance",
        all_properties=get_properties(conn), active_property=None,
        current_month=f"{MONTH_NAMES[month]} {year}", rows=rows,
        heatmap_months=[MONTH_ABBR[int(ym.split('-')[1])] + " " + ym.split('-')[0][2:] for ym in heatmap_months],
        heatmap_rows=heatmap_rows,
        months_json=json.dumps(months), portfolio_json=json.dumps(portfolio_series),
    )
