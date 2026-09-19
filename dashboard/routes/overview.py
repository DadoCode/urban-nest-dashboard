import json

from flask import Blueprint, render_template, request

import db
import services.kpis as kpis
from services.common import get_properties, pct_delta, yoy_pairs
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

    # A prior-period/prior-year base below this is too small for a percent
    # swing off it to mean anything -- "+8490%" off a near-zero base is
    # noise, not signal, so it's suppressed rather than shown capped or
    # literal (the tile just omits that delta line instead). Net profit's
    # threshold is much higher than revenue's: a thin-margin month can
    # have a genuinely small profit base (a real month, not "no data"),
    # and revenue growth alone can make profit swing by thousands of
    # percent off it without that being the meaningful story.
    MIN_BASE = {"revenue": 100, "net_profit": 1000, "adr": 20, "revpar": 20,
                "occupancy": 0.05, "margin": 0.05, "booked_nights": 2, "avg_stay": 0.5}

    def tile(label, key, value_fmt, extra_note=None, cur_val=None, prev_val=None):
        cv = cur_val if cur_val is not None else cur[key]
        pv = prev_val if prev_val is not None else prev[key]
        lv = (last_year[key] if key in last_year.keys() else None) if cur_val is None else None
        min_base = MIN_BASE.get(key, 0)
        delta = pct_delta(cv, pv, min_base=min_base)
        delta_ly = pct_delta(cv, lv, min_base=min_base) if lv is not None else None
        return {
            "label": label, "value": value_fmt(cv),
            "delta": delta,
            "delta_ly": delta_ly,
            "note": extra_note,
        }

    rev_target = kpis.dynamic_target(conn, property_id, ctx["start_year"], ctx["start_month"],
                                      ctx["end_year"], ctx["end_month"], "revenue")
    primary = [
        tile(f"Revenue — {period_label}", "revenue", lambda v: f"£{v:,.0f}",
             (f"{cur['revenue'] / rev_target * 100:.0f}% of £{rev_target:,.0f} trailing-average target" if rev_target else None)),
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
    targets into one ambiguous figure. Targets are computed from each
    property's own trailing 3-month average rather than a stored number
    someone typed in once -- a hand-set target from the original Excel
    import stops meaning anything once the business has grown past it
    (routinely showing "400% of target"); a trailing average self-adjusts
    every month instead of going stale."""
    args = (ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    rev_target = kpis.dynamic_target(conn, property_id, *args, "revenue")
    profit_target = kpis.dynamic_target(conn, property_id, *args, "net_profit")
    occ_target = kpis.dynamic_target(conn, property_id, *args, "occupancy")

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
        occ_target_pct = occ_target * 100
        bars.append({"label": "Occupancy", "actual": cur_occ_pct, "target": occ_target_pct,
                      "pct": min(round(cur_occ_pct / occ_target_pct * 100), 999) if occ_target_pct else 0,
                      "actual_fmt": f"{cur_occ_pct:.0f}%", "target_fmt": f"{occ_target_pct:.0f}%"})
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
        target = kpis.dynamic_target(conn, p["id"], ctx["start_year"], ctx["start_month"],
                                      ctx["end_year"], ctx["end_month"], "revenue")
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

    # Trailing 12 months by default (brief §6), regardless of how much
    # history exists -- a 3-year line is unreadable as the main chart.
    # Anchored at ctx's end month, not just "whatever the last entry in
    # monthly_series happens to be" -- a barely-started current month with
    # one stray transaction would otherwise show up as a misleading cliff
    # down to near-zero at the end of the line.
    anchor_ym = f"{ctx['end_year']}-{ctx['end_month']:02d}"
    portfolio_series = [s for s in kpis.monthly_series(conn, ctx["property_id"]) if s["ym"] <= anchor_ym][-12:]
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
        costs_json=json.dumps([s["costs"] for s in portfolio_series]),
        profit_json=json.dumps([s["net_profit"] for s in portfolio_series]),
        margin_json=json.dumps([round(s["margin"] * 100, 1) for s in portfolio_series]),
        occupancy_json=json.dumps([round(s["occupancy"] * 100, 1) for s in portfolio_series]),
    )
