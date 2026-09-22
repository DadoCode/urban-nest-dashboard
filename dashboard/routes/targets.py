import datetime
import json
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

import db
import services.kpis as kpis
from services.context import request_context
from services.common import MONTH_NAMES, get_properties, get_property

bp = Blueprint("targets", __name__)

YM = re.compile(r"^\d{4}-\d{2}$")
BASES = [("all", "All recorded months"), ("12", "Last 12 months"), ("6", "Last 6 months"), ("3", "Last 3 months")]
METRICS = ("revenue", "profit", "occupancy")
COLUMN = {"revenue": "revenue_target", "profit": "profit_target", "occupancy": "occupancy_target"}


def _basis(args):
    b = args.get("basis", "all")
    return b if b in {k for k, _ in BASES} else "all"


def _month(args, cur):
    m = args.get("month", cur)
    return m if YM.match(m or "") and m <= cur else cur


def _label(ym):
    return f"{MONTH_NAMES[int(ym[5:])]} {ym[:4]}"


def _num(raw):
    raw = (raw or "").replace(",", "").replace("£", "").replace("%", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _history(conn, property_id, cur):
    series = [r for r in kpis.monthly_series(conn, property_id) if r["ym"] <= cur and r["revenue"] > 0]
    return {r["ym"]: {"revenue": round(r["revenue"], 2), "profit": round(r["net_profit"], 2),
                      "occupancy": round(r["occupancy"] * 100, 1)} for r in series}


def _metric_view(key, saved_row, avg, hist, month):
    saved_val = saved_row[COLUMN[key]] if saved_row else None
    avg_val = avg[key] if avg else None
    target = saved_val if saved_val is not None else avg_val
    actual = hist.get(month, {}).get(key)
    ly = hist.get(f"{int(month[:4]) - 1}-{month[5:]}", {}).get(key)
    pct = round(actual / target * 100) if (actual is not None and target) else None
    return {"saved": saved_val is not None, "target": target, "avg": avg_val, "actual": actual,
            "last_year": ly, "pct": pct,
            "remaining": (target - actual) if (actual is not None and target) else None}


def _property_view(conn, p, saved_row, months, month, cur):
    hist = _history(conn, p["id"], cur)
    avg = kpis.data_average(conn, p["id"], months)
    return {"id": p["id"], "name": p["name"], "hist": hist, "avg_months": avg["months"] if avg else 0,
            "metrics": {k: _metric_view(k, saved_row, avg, hist, month) for k in METRICS}}


@bp.route("/targets")
def index():
    conn = db.get_conn()
    basis = _basis(request.args)
    months = None if basis == "all" else int(basis)
    cy, cm = kpis.current_period(conn)
    cur = f"{cy}-{cm:02d}"
    ctx = request_context(conn)
    # This page has no property selector (hide_property=True below) and
    # always lists every property, so a stray "property" left over from
    # browsing elsewhere shouldn't make "Reset to latest" appear here --
    # only period/compare matter on this page, same override
    # request_context() already applies for a fixed-property workspace.
    ctx["is_latest"] = ctx["period_is_latest"] and ctx["compare"] == "previous_period"
    month = f"{ctx['end_year']}-{ctx['end_month']:02d}"
    if month > cur:
        month = cur

    # A property arrived from (e.g. its workspace's "Targets" link) doesn't
    # filter this portfolio-wide table -- it just gets visually focused,
    # so the table still gives full context while making it obvious which
    # row you came for.
    focus_property = request.args.get("property") or None

    saved = {r["property_id"]: r for r in conn.execute("SELECT * FROM property_targets")}
    rows, month_set = [], set()
    for p in get_properties(conn, include_overhead=False):
        view = _property_view(conn, p, saved.get(p["id"]), months, month, cur)
        month_set.update(view["hist"])
        rows.append(view)

    totals = {}
    for key in ("revenue", "profit"):
        counted = [r["metrics"][key] for r in rows if r["metrics"][key]["target"]]
        target = sum(m["target"] for m in counted)
        actual = sum(m["actual"] or 0 for m in counted)
        totals[key] = {"target": target, "actual": actual, "pct": round(actual / target * 100) if target else None,
                       "remaining": target - actual, "n": len(counted)}

    return render_template(
        "targets.html", active="targets", all_properties=get_properties(conn), active_property=None,
        rows=rows, totals=totals, bases=BASES, basis=basis, month=month, month_label=_label(month),
        any_saved=bool(saved), context_bar=True, ctx=ctx, hide_property=True, hide_compare=True,
        multi_month=(ctx["start_year"], ctx["start_month"]) != (ctx["end_year"], ctx["end_month"]),
        focus_property=focus_property,
    )


@bp.route("/targets/<property_id>/edit")
def edit(property_id):
    conn = db.get_conn()
    p = get_property(conn, property_id)
    if not p or p["type"] == "overhead":
        abort(404)
    basis = _basis(request.args)
    months = None if basis == "all" else int(basis)
    cy, cm = kpis.current_period(conn)
    cur = f"{cy}-{cm:02d}"
    month = _month(request.args, cur)
    saved_row = conn.execute("SELECT * FROM property_targets WHERE property_id=?", (property_id,)).fetchone()
    view = _property_view(conn, p, saved_row, months, month, cur)
    avg_label = "Average of all recorded months" if basis == "all" else f"{basis}-month average"
    return render_template(
        "partials/target_drawer.html", p=view, month=month, month_label=_label(month), basis=basis,
        avg_label=avg_label, history_json=json.dumps(view["hist"]),
        ly_label=f"{MONTH_NAMES[int(month[5:])]} {int(month[:4]) - 1}",
    )


@bp.route("/targets/<property_id>/save", methods=["POST"])
def save(property_id):
    conn = db.get_conn()
    p = get_property(conn, property_id)
    if not p or p["type"] == "overhead":
        abort(404)
    vals = [_num(request.form.get(k)) for k in METRICS]
    if vals[2] is not None:
        vals[2] = min(vals[2], 100.0)
    if all(v is None for v in vals):
        conn.execute("DELETE FROM property_targets WHERE property_id=?", (property_id,))
    else:
        conn.execute(
            """INSERT INTO property_targets (property_id, revenue_target, profit_target, occupancy_target, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(property_id) DO UPDATE SET revenue_target=excluded.revenue_target,
               profit_target=excluded.profit_target, occupancy_target=excluded.occupancy_target,
               updated_at=excluded.updated_at""",
            (property_id, *vals, datetime.datetime.now().isoformat(timespec="seconds")),
        )
    conn.commit()
    flash(f"\u2713 Targets saved for {p['name']}.", "success")
    return redirect(url_for("targets.index", basis=_basis(request.form)))


@bp.route("/targets/<property_id>/reset", methods=["POST"])
def reset_one(property_id):
    conn = db.get_conn()
    p = get_property(conn, property_id)
    if not p:
        abort(404)
    conn.execute("DELETE FROM property_targets WHERE property_id=?", (property_id,))
    conn.commit()
    flash(f"\u2713 {p['name']} is back on its data average.", "success")
    return redirect(url_for("targets.index", basis=_basis(request.form)))


@bp.route("/targets/reset", methods=["POST"])
def reset():
    conn = db.get_conn()
    conn.execute("DELETE FROM property_targets")
    conn.commit()
    flash("\u2713 All targets are back on their data averages.", "success")
    return redirect(url_for("targets.index", basis=_basis(request.form)))
