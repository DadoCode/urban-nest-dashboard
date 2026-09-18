import json

from flask import Blueprint, render_template, request

import db
import services.kpis as kpis
from services.common import get_properties, pct_delta, target_row, target_total, yoy_pairs
from services.completeness import completeness_for, DOC_TYPE_LABELS
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


def _kpi_rows(conn, property_id, ctx):
    """Four primary KPIs, visually dominant, plus a quieter secondary strip
    -- replaces the old 7-tile row where every metric got equal weight."""
    cur, prev, last_year = _range_snapshot(conn, property_id, ctx)
    period_label = ("MTD, " if ctx["partial"] and ctx["choice"] == "this_month" else "") + ctx["display"]
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    avg_stay = kpis.avg_stay(conn, property_id, start, end)
    prev_start, prev_end = kpis.range_bounds(*kpis.prior_period(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
    prev_avg_stay = kpis.avg_stay(conn, property_id, prev_start, prev_end)

    def tile(label, key, value_fmt, extra_note=None, cur_val=None, prev_val=None):
        cv = cur_val if cur_val is not None else cur[key]
        pv = prev_val if prev_val is not None else prev[key]
        lv = (last_year[key] if key in last_year.keys() else None) if cur_val is None else None
        return {
            "label": label, "value": value_fmt(cv),
            "delta": pct_delta(cv, pv),
            "delta_ly": pct_delta(cv, lv) if lv is not None else None,
            "note": extra_note,
        }

    rev_target = target_total(conn, property_id, ctx, "revenue_target")
    primary = [
        tile(f"Revenue — {period_label}", "revenue", lambda v: f"£{v:,.0f}",
             (f"{cur['revenue'] / rev_target * 100:.0f}% of £{rev_target:,.0f} target" if rev_target else None)),
        tile("Net profit", "net_profit", lambda v: f"£{v:,.0f}"),
        tile("Occupancy", "occupancy", lambda v: f"{v * 100:.0f}%"),
        tile("RevPAR", "revpar", lambda v: f"£{v:,.0f}"),
    ]
    secondary = [
        tile("ADR", "adr", lambda v: f"£{v:,.0f}"),
        tile("Booked nights", "booked_nights", lambda v: f"{v:,.0f}"),
        tile("Profit margin", "margin", lambda v: f"{v * 100:.0f}%"),
        tile("Avg stay", "avg_stay", lambda v: f"{v:.1f} nights", cur_val=avg_stay, prev_val=prev_avg_stay),
    ]
    return primary, secondary, cur


def _target_bars(conn, property_id, ctx, cur):
    """Distinct bars per metric -- never merge revenue/profit/occupancy
    targets into one ambiguous figure."""
    rev_target = target_total(conn, property_id, ctx, "revenue_target")
    profit_target = target_total(conn, property_id, ctx, "profit_target")
    if property_id:
        occ_row = target_row(conn, property_id, ctx["end_year"], ctx["end_month"])
        occ_target = (occ_row["occupancy_target"] if occ_row else None)
    else:
        occ_rows = conn.execute(
            "SELECT occupancy_target FROM targets WHERE year=? AND month=? AND occupancy_target IS NOT NULL",
            (ctx["end_year"], ctx["end_month"]),
        ).fetchall()
        occ_target = (sum(r["occupancy_target"] for r in occ_rows) / len(occ_rows)) if occ_rows else None

    bars = []
    if rev_target:
        bars.append({"label": "Revenue", "actual": cur["revenue"], "target": rev_target,
                      "pct": min(round(cur["revenue"] / rev_target * 100), 999),
                      "actual_fmt": f"£{cur['revenue']:,.0f}", "target_fmt": f"£{rev_target:,.0f}"})
    if profit_target:
        bars.append({"label": "Profit", "actual": cur["net_profit"], "target": profit_target,
                      "pct": min(round(cur["net_profit"] / profit_target * 100), 999),
                      "actual_fmt": f"£{cur['net_profit']:,.0f}", "target_fmt": f"£{profit_target:,.0f}"})
    if occ_target:
        cur_occ_pct = cur["occupancy"] * 100
        bars.append({"label": "Occupancy", "actual": cur_occ_pct, "target": occ_target,
                      "pct": min(round(cur_occ_pct / occ_target * 100), 999),
                      "actual_fmt": f"{cur_occ_pct:.0f}%", "target_fmt": f"{occ_target:.0f}%"})
    return bars


def _completeness_summary(conn, flats):
    """Current-calendar-month rollup -- deliberately not tied to whatever
    date range the context bar has selected, since "data completeness" is
    a monthly-cadence concept regardless of what period you're analysing."""
    year, month = kpis.current_period(conn)
    start, end = kpis.month_bounds(year, month)
    rows = []
    for p in flats:
        c = completeness_for(conn, p["id"], start, end)
        if c:
            rows.append({"name": p["name"], "id": p["id"], **c})
    if not rows:
        return None
    overall = round(sum(r["pct"] for r in rows) / len(rows))
    gaps = [r for r in rows if r["pct"] < 100]
    return {"overall": overall, "rows": rows, "gaps": gaps, "month_label": None, "year": year, "month": month}


@bp.route("/")
def index():
    conn = db.get_conn()
    nav_properties = get_properties(conn)
    flats = get_properties(conn, include_overhead=False)
    ctx = resolve_context(conn, request.args)
    viewing = next((p for p in nav_properties if p["id"] == ctx["property_id"]), None) if ctx["property_id"] else None
    primary_tiles, secondary_tiles, cur = _kpi_rows(conn, ctx["property_id"], ctx)
    target_bars = _target_bars(conn, ctx["property_id"], ctx, cur)

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
    completeness = _completeness_summary(conn, insight_scope)

    portfolio_series = kpis.monthly_series(conn, ctx["property_id"])
    yoy = yoy_pairs(conn, ctx["property_id"], (ctx["end_year"], ctx["end_month"]))
    overhead_property = next((p for p in nav_properties if p["type"] == "overhead"), None)

    return render_template(
        "index.html", active="overview", all_properties=nav_properties, flats_count=len(flats),
        active_property=ctx["property_id"], viewing=viewing, overhead_property=overhead_property,
        primary_tiles=primary_tiles, secondary_tiles=secondary_tiles, target_bars=target_bars, ctx=ctx,
        context_bar=True, prop_rows=prop_rows, yoy=yoy, expense_rows=expense_rows,
        insights=insights, completeness=completeness, doc_type_labels=DOC_TYPE_LABELS,
        months_json=json.dumps([s["ym"] for s in portfolio_series]),
        income_json=json.dumps([s["revenue"] for s in portfolio_series]),
        profit_json=json.dumps([s["net_profit"] for s in portfolio_series]),
        occupancy_json=json.dumps([round(s["occupancy"] * 100, 1) for s in portfolio_series]),
    )
