import json

from flask import Blueprint, render_template, request

import db
import services.kpis as kpis
from services.common import get_properties, pct_delta, target_total
from services.context import resolve_context
from services.insights import compute_insights

bp = Blueprint("overview", __name__)


def _range_snapshot(conn, property_id, ctx):
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    cur = kpis.kpi_snapshot(conn, property_id, start, end)
    pstart, pend = kpis.range_bounds(*kpis.prior_period(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
    prev = kpis.kpi_snapshot(conn, property_id, pstart, pend)
    lystart, lyend = kpis.range_bounds(*kpis.same_period_last_year(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
    last_year = kpis.kpi_snapshot(conn, property_id, lystart, lyend)
    return cur, prev, last_year


def _kpi_row(conn, property_id, ctx):
    cur, prev, last_year = _range_snapshot(conn, property_id, ctx)
    period_label = ("MTD, " if ctx["partial"] and ctx["choice"] == "this_month" else "") + ctx["display"]

    def tile(label, key, value_fmt, extra_note=None):
        return {
            "label": label, "value": value_fmt(cur[key]),
            "delta": pct_delta(cur[key], prev[key]),
            "delta_ly": pct_delta(cur[key], last_year[key]),
            "note": extra_note,
        }

    rev_target = target_total(conn, property_id, ctx, "revenue_target")
    profit_target = target_total(conn, property_id, ctx, "profit_target")
    tiles = [
        tile(f"Revenue — {period_label}", "revenue", lambda v: f"£{v:,.0f}",
             (f"{cur['revenue'] / rev_target * 100:.0f}% of £{rev_target:,.0f} target" if rev_target else None)),
        tile("Net profit", "net_profit", lambda v: f"£{v:,.0f}",
             (f"{cur['net_profit'] / profit_target * 100:.0f}% of £{profit_target:,.0f} target" if profit_target else None)),
        tile("Margin", "margin", lambda v: f"{v * 100:.0f}%"),
        tile("Occupancy", "occupancy", lambda v: f"{v * 100:.0f}%"),
        tile("Booked nights", "booked_nights", lambda v: f"{v:,.0f}"),
        tile("ADR", "adr", lambda v: f"£{v:,.0f}"),
        tile("RevPAR", "revpar", lambda v: f"£{v:,.0f}"),
    ]
    return tiles, cur


@bp.route("/")
def index():
    conn = db.get_conn()
    nav_properties = get_properties(conn)
    flats = get_properties(conn, include_overhead=False)
    ctx = resolve_context(conn, request.args)
    viewing = next((p for p in nav_properties if p["id"] == ctx["property_id"]), None) if ctx["property_id"] else None
    tiles, cur = _kpi_row(conn, ctx["property_id"], ctx)

    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    prop_rows = []
    for p in flats:
        snap = kpis.kpi_snapshot(conn, p["id"], start, end)
        target = target_total(conn, p["id"], ctx, "revenue_target")
        prop_rows.append({
            "id": p["id"], "name": p["name"],
            "revenue": snap["revenue"], "profit": snap["net_profit"], "margin": snap["margin"],
            "occupancy": snap["occupancy"], "adr": snap["adr"],
            "target": target,
            "vs_target": round(snap["revenue"] / target * 100, 1) if target else None,
        })
    prop_rows.sort(key=lambda r: r["revenue"], reverse=True)

    expense_scope = "AND property_id=?" if ctx["property_id"] else ""
    expense_params = (ctx["property_id"],) if ctx["property_id"] else ()
    expense_categories = conn.execute(
        f"""SELECT category, SUM(amount) amt FROM transactions
           WHERE direction='expense' AND category != 'reconciliation' AND date>=? AND date<? {expense_scope}
           GROUP BY category ORDER BY amt DESC LIMIT 8""",
        (start, end, *expense_params),
    ).fetchall()
    pstart, pend = kpis.range_bounds(*kpis.prior_period(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
    prev_by_category = {r["category"]: r["amt"] for r in conn.execute(
        f"""SELECT category, SUM(amount) amt FROM transactions
           WHERE direction='expense' AND category != 'reconciliation' AND date>=? AND date<? {expense_scope} GROUP BY category""",
        (pstart, pend, *expense_params),
    ).fetchall()}
    expense_rows = [{"category": r["category"], "amount": r["amt"],
                      "delta": pct_delta(r["amt"], prev_by_category.get(r["category"]))} for r in expense_categories]

    insight_scope = [p for p in flats if p["id"] == ctx["property_id"]] if ctx["property_id"] else flats
    insights = compute_insights(conn, insight_scope, ctx)

    portfolio_series = kpis.monthly_series(conn, ctx["property_id"])
    from services.common import yoy_pairs
    yoy = yoy_pairs(conn, ctx["property_id"], (ctx["end_year"], ctx["end_month"]))
    overhead_property = next((p for p in nav_properties if p["type"] == "overhead"), None)

    return render_template(
        "index.html", active="overview", all_properties=nav_properties, flats_count=len(flats),
        active_property=ctx["property_id"], viewing=viewing, overhead_property=overhead_property,
        tiles=tiles, ctx=ctx,
        context_bar=True, prop_rows=prop_rows, yoy=yoy, expense_rows=expense_rows, insights=insights,
        months_json=json.dumps([s["ym"] for s in portfolio_series]),
        income_json=json.dumps([s["revenue"] for s in portfolio_series]),
        profit_json=json.dumps([s["net_profit"] for s in portfolio_series]),
        occupancy_json=json.dumps([round(s["occupancy"] * 100, 1) for s in portfolio_series]),
    )
