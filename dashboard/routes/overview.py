import json

from flask import Blueprint, render_template, request

import db
import services.kpis as kpis
from services.common import get_properties, pct_delta
from services.context import request_context, range_params

bp = Blueprint("overview", __name__)


def _range_snapshot(conn, property_id, ctx):
    # adjusted_kpi_snapshot(): Revenue/Net profit/Margin reflect only what
    # this business actually earns (full revenue for owned flats, the fee
    # share for managed ones) -- Occupancy/Booked nights/ADR/RevPAR are
    # unaffected, computed the same way as kpi_snapshot().
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    cur = kpis.adjusted_kpi_snapshot(conn, property_id, start, end)
    pstart, pend = kpis.range_bounds(*kpis.prior_period(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
    prev = kpis.adjusted_kpi_snapshot(conn, property_id, pstart, pend)
    lystart, lyend = kpis.range_bounds(*kpis.same_period_last_year(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"]))
    last_year = kpis.adjusted_kpi_snapshot(conn, property_id, lystart, lyend)
    return cur, prev, last_year


def kpi_rows(conn, property_id, ctx):
    """Four primary KPIs, visually dominant, plus a quieter secondary strip
    -- replaces the old 7-tile row where every metric got equal weight.
    Shared with routes/properties.py so a property workspace's own
    Overview uses the exact same grouping as the portfolio one."""
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
            "key": key, "label": label, "value": value_fmt(cv),
            "delta": delta,
            "delta_ly": delta_ly,
            "note": extra_note,
        }

    primary = [
        tile(f"Revenue — {period_label}", "revenue", lambda v: f"£{v:,.0f}"),
        tile("Property Profit", "net_profit", lambda v: f"£{v:,.0f}"),
        tile("Occupancy", "occupancy", lambda v: f"{v * 100:.0f}%"),
        tile("RevPAR", "revpar", lambda v: f"£{v:,.0f}"),
    ]
    secondary = [
        tile("ADR", "adr", lambda v: f"£{v:,.0f}"),
        tile("Booked nights", "booked_nights", lambda v: f"{v:,.0f}"),
        tile("Profit margin", "margin", lambda v: f"{v * 100:.0f}%"),
        tile("Avg stay", "avg_stay", lambda v: f"{v:.1f} nights" if v else "—", cur_val=avg_stay, prev_val=prev_avg_stay),
    ]
    return primary, secondary, cur




@bp.route("/")
def index():
    conn = db.get_conn()
    nav_properties = get_properties(conn)
    flats = get_properties(conn, include_overhead=False)
    ctx = request_context(conn)
    viewing = next((p for p in nav_properties if p["id"] == ctx["property_id"]), None) if ctx["property_id"] else None
    primary_tiles, _secondary_tiles, cur = kpi_rows(conn, ctx["property_id"], ctx)

    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])

    # Rent-to-rent/owned and managed flats run on different economics (see
    # services/kpis.py's adjusted_revenue/business_income) -- mixing them
    # into one table with the same columns either misrepresents a managed
    # flat's gross booking revenue as "Urban Nest's revenue" or shows a
    # meaningless Margin, so they're presented separately.
    rtr_rows, managed_rows = [], []
    for p in flats:
        if p["management_fee_pct"]:
            snap = kpis.adjusted_kpi_snapshot(conn, p["id"], start, end)
            managed_rows.append({
                "id": p["id"], "name": p["name"], "fee": snap["net_profit"],
                "occupancy": snap["occupancy"], "adr": snap["adr"],
            })
        else:
            snap = kpis.kpi_snapshot(conn, p["id"], start, end)
            rtr_rows.append({
                "id": p["id"], "name": p["name"], "revenue": snap["revenue"], "costs": snap["costs"],
                "profit": snap["net_profit"], "occupancy": snap["occupancy"], "adr": snap["adr"],
            })
    rtr_rows.sort(key=lambda r: r["revenue"], reverse=True)
    managed_rows.sort(key=lambda r: r["fee"], reverse=True)

    # Trailing 12 months by default, regardless of how much history exists
    # -- a 3-year line is unreadable as the main chart. Anchored at ctx's
    # end month, not just "whatever the last entry in monthly_series
    # happens to be" -- a barely-started current month with one stray
    # transaction would otherwise show up as a misleading cliff down to
    # near-zero at the end of the line.
    anchor_ym = f"{ctx['end_year']}-{ctx['end_month']:02d}"
    portfolio_series = [s for s in kpis.adjusted_monthly_series(conn, ctx["property_id"]) if s["ym"] <= anchor_ym]
    occupancy_by_property = {
        p["id"]: {"name": p["name"], "values": {s["ym"]: round(s["occupancy"] * 100, 1)
                                                  for s in kpis.monthly_series(conn, p["id"]) if s["ym"] <= anchor_ym}}
        for p in flats
    }

    return render_template(
        "index.html", active="overview", all_properties=nav_properties, flats_count=len(flats),
        active_property=ctx["property_id"], viewing=viewing,
        primary_tiles=primary_tiles, ctx=ctx,
        context_bar=True, rtr_rows=rtr_rows, managed_rows=managed_rows,
        ctx_params=range_params(ctx),
        series_json=json.dumps({
            "months": [s["ym"] for s in portfolio_series],
            "income": [round(s["revenue"], 2) for s in portfolio_series],
            "costs": [round(s["costs"], 2) for s in portfolio_series],
            "profit": [round(s["net_profit"], 2) for s in portfolio_series],
            "margin": [round(s["margin"] * 100, 1) for s in portfolio_series],
            "occupancy": [round(s["occupancy"] * 100, 1) for s in portfolio_series],
        }),
        occ_props_json=json.dumps(occupancy_by_property),
        anchor_ym=anchor_ym,
    )
