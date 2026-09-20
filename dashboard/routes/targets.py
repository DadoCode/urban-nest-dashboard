import datetime
import json
import re

from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
import services.kpis as kpis
from services.common import MONTH_NAMES, get_properties

bp = Blueprint("targets", __name__)

YM = re.compile(r"^\d{4}-\d{2}$")
BASES = [("all", "All available months"), ("12", "Last 12 months"), ("6", "Last 6 months"), ("3", "Last 3 months")]
METRICS = [("revenue", "Revenue", "£"), ("profit", "Net profit", "£"), ("occupancy", "Occupancy", "%")]


def _basis(args):
    b = args.get("basis", "all")
    return b if b in {k for k, _ in BASES} else "all"


def _num(raw):
    raw = (raw or "").replace(",", "").replace("£", "").replace("%", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


@bp.route("/targets")
def index():
    conn = db.get_conn()
    basis = _basis(request.args)
    months = None if basis == "all" else int(basis)
    cy, cm = kpis.current_period(conn)
    cur = f"{cy}-{cm:02d}"
    month = request.args.get("month", cur)
    if not YM.match(month) or month > cur:
        month = cur

    flats = get_properties(conn, include_overhead=False)
    saved = {r["property_id"]: r for r in conn.execute("SELECT * FROM property_targets")}

    rows, history, month_set = [], {}, set()
    for p in flats:
        avg = kpis.data_average(conn, p["id"], months)
        s = saved.get(p["id"])
        series = [r for r in kpis.monthly_series(conn, p["id"]) if r["ym"] <= cur and r["revenue"] > 0]
        history[p["id"]] = {r["ym"]: {"revenue": round(r["revenue"], 2), "profit": round(r["net_profit"], 2),
                                      "occupancy": round(r["occupancy"] * 100, 1)} for r in series}
        month_set.update(history[p["id"]])
        entry = {"id": p["id"], "name": p["name"], "avg": avg, "metrics": {}}
        for key, _, _ in METRICS:
            col = {"revenue": "revenue_target", "profit": "profit_target", "occupancy": "occupancy_target"}[key]
            saved_val = s[col] if s else None
            avg_val = avg[key] if avg else None
            entry["metrics"][key] = {
                "saved": saved_val is not None,
                "value": saved_val if saved_val is not None else avg_val,
                "avg": avg_val,
            }
        rows.append(entry)

    return render_template(
        "targets.html", active="targets", all_properties=get_properties(conn), active_property=None,
        rows=rows, metrics=METRICS, bases=BASES, basis=basis, month=month,
        month_label=f"{MONTH_NAMES[int(month[5:])]} {month[:4]}",
        month_options=[{"ym": m, "label": f"{MONTH_NAMES[int(m[5:])]} {m[:4]}"} for m in sorted(month_set, reverse=True)[:24]],
        history_json=json.dumps(history),
    )


@bp.route("/targets/save", methods=["POST"])
def save():
    conn = db.get_conn()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for p in get_properties(conn, include_overhead=False):
        vals = [_num(request.form.get(f"{k}_{p['id']}")) for k in ("revenue", "profit", "occupancy")]
        if vals[2] is not None:
            vals[2] = min(vals[2], 100.0)
        if all(v is None for v in vals):
            conn.execute("DELETE FROM property_targets WHERE property_id=?", (p["id"],))
            continue
        conn.execute(
            """INSERT INTO property_targets (property_id, revenue_target, profit_target, occupancy_target, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(property_id) DO UPDATE SET revenue_target=excluded.revenue_target,
               profit_target=excluded.profit_target, occupancy_target=excluded.occupancy_target,
               updated_at=excluded.updated_at""",
            (p["id"], *vals, now),
        )
    conn.commit()
    flash("Targets saved.", "success")
    return redirect(url_for("targets.index", basis=_basis(request.form), month=request.form.get("month", "")))


@bp.route("/targets/reset", methods=["POST"])
def reset():
    conn = db.get_conn()
    conn.execute("DELETE FROM property_targets")
    conn.commit()
    flash("All targets reset to the data average.", "success")
    return redirect(url_for("targets.index", basis=_basis(request.form)))
