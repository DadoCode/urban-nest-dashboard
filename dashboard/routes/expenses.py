import json

from flask import Blueprint, render_template

import db
import services.kpis as kpis
from services.common import MONTH_NAMES, get_properties

bp = Blueprint("expenses", __name__)


@bp.route("/expenses")
def index():
    conn = db.get_conn()
    flats = get_properties(conn, include_overhead=False)
    year, month = kpis.current_period(conn)
    start, end = kpis.month_bounds(year, month)
    months = kpis.months_with_data(conn, None)

    rows = []
    for p in flats:
        opex = kpis.costs(conn, p["id"], start, end, capex=False)
        capex = kpis.costs(conn, p["id"], start, end, capex=True)
        rows.append({"id": p["id"], "name": p["name"], "opex": opex, "capex": capex, "total_costs": opex + capex})
    rows.sort(key=lambda r: r["total_costs"], reverse=True)

    opex_series, capex_series = [], []
    for ym in months:
        y, m = map(int, ym.split("-"))
        s, e = kpis.month_bounds(y, m)
        opex_series.append(kpis.costs(conn, None, s, e, capex=False))
        capex_series.append(kpis.costs(conn, None, s, e, capex=True))

    def category_breakdown(cstart=None, cend=None):
        clause, params = ("AND date>=? AND date<?", (cstart, cend)) if cstart else ("", ())
        return conn.execute(
            f"""SELECT category, SUM(amount) amt, COUNT(*) n FROM transactions
                WHERE direction='expense' AND category != 'reconciliation' {clause}
                GROUP BY category ORDER BY amt DESC""",
            params,
        ).fetchall()

    vendor_breakdown = conn.execute(
        """SELECT COALESCE(NULLIF(vendor,''),'(no vendor)') vendor, SUM(amount) amt, COUNT(*) n
           FROM transactions WHERE direction='expense' AND category != 'reconciliation' AND date>=? AND date<?
           GROUP BY vendor ORDER BY amt DESC LIMIT 12""",
        (start, end),
    ).fetchall()

    total_opex = kpis.costs(conn, None, start, end, capex=False)
    total_capex = kpis.costs(conn, None, start, end, capex=True)
    cleaning = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE direction='expense' AND category='cleaning' AND date>=? AND date<?",
        (start, end),
    ).fetchone()[0]
    overhead_prop = conn.execute("SELECT id FROM properties WHERE type='overhead' LIMIT 1").fetchone()
    overhead_cost = kpis.costs(conn, overhead_prop["id"], start, end) if overhead_prop else 0
    nights = kpis.booked_nights(conn, None, start, end)
    cost_per_night = (total_opex + total_capex) / nights if nights else 0

    tiles = [
        {"label": "Total costs", "value": f"£{total_opex + total_capex:,.0f}"},
        {"label": "Opex", "value": f"£{total_opex:,.0f}"},
        {"label": "Capex", "value": f"£{total_capex:,.0f}"},
        {"label": "Cleaning", "value": f"£{cleaning:,.0f}"},
        {"label": "Portfolio overhead", "value": f"£{overhead_cost:,.0f}"},
        {"label": "Cost / booked night", "value": f"£{cost_per_night:,.0f}"},
    ]

    return render_template(
        "expenses.html", active="expenses", all_properties=get_properties(conn), active_property=None,
        current_month=f"{MONTH_NAMES[month]} {year}", rows=rows, tiles=tiles, vendor_breakdown=vendor_breakdown,
        this_month_categories=category_breakdown(start, end), all_time_categories=category_breakdown(),
        months_json=json.dumps(months), opex_json=json.dumps(opex_series), capex_json=json.dumps(capex_series),
    )
