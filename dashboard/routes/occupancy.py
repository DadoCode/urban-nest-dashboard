import json

from flask import Blueprint, render_template

import db
import services.kpis as kpis
from services.common import MONTH_ABBR, MONTH_NAMES, get_properties, pct_delta

bp = Blueprint("occupancy", __name__)


@bp.route("/occupancy")
def index():
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
        "occupancy.html", active="occupancy", all_properties=get_properties(conn), active_property=None,
        current_month=f"{MONTH_NAMES[month]} {year}", rows=rows,
        heatmap_months=[MONTH_ABBR[int(ym.split('-')[1])] + " " + ym.split('-')[0][2:] for ym in heatmap_months],
        heatmap_rows=heatmap_rows,
        months_json=json.dumps(months), portfolio_json=json.dumps(portfolio_series),
    )
