"""Reconciliation: for each property and month, compare the Excel history with the
reservations uploaded so far, and let a person -- explicitly, after seeing the
projected impact -- choose which one feeds the dashboard. Plus a plain figure audit
that answers "where did this number come from?"."""
import csv
import io

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

import db
import services.kpis as kpis
import services.reconcile as rc
import services.sources as src
from services.common import get_properties, get_property
from routes.expenses import _costs

bp = Blueprint("reconcile", __name__)

ROWS = [("gbr", "Gross Booking Revenue", "money"), ("nights", "Booked nights", "int"), ("reservations", "Reservations", "int"),
        ("occupancy", "Occupancy", "pct"), ("adr", "ADR", "money"), ("revpar", "RevPAR", "money")]
IMPACT_ROWS = ROWS[:1] + ROWS[1:2] + ROWS[3:] + [("un_revenue", "Urban Nest Revenue", "money"), ("profit", "Property Profit (Urban Nest)", "money")]


def _valid_month(ym):
    return bool(ym) and len(ym) == 7 and ym[4] == "-" and ym[:4].isdigit() and ym[5:].isdigit() and 1 <= int(ym[5:]) <= 12


@bp.route("/reconciliation")
def index():
    conn = db.get_conn()
    rows = rc.candidates(conn)
    needed = sum(1 for r in rows if r["status"]["label"] == "RECONCILIATION NEEDED")
    only_detailed = sorted(rc.detailed_months(conn) - {(r["property_id"], r["ym"]) for r in rows})
    names = {p["id"]: p["name"] for p in get_properties(conn)}
    no_excel = [{"property": names.get(p, p), "label": rc.month_label(ym)} for p, ym in only_detailed if (p, ym) not in rc._all_legacy_months(conn)]
    return render_template("reconciliation.html", active="documents", all_properties=get_properties(conn), active_property=None,
                           rows=rows, needed=needed, no_excel=no_excel)


@bp.route("/reconciliation/<property_id>/<ym>")
def month(property_id, ym):
    conn = db.get_conn()
    prop = get_property(conn, property_id)
    if not prop or not _valid_month(ym):
        flash("We couldn't find that property or month.", "error")
        return redirect(url_for("reconcile.index"))
    existing, uploaded = rc.metrics(conn, property_id, ym, src.LEGACY), rc.metrics(conn, property_id, ym, src.DETAILED)
    status = rc.status_of(conn, property_id, ym)
    current = existing if status["active"] == src.LEGACY else uploaded
    after = uploaded if status["active"] == src.LEGACY else existing      # the other source
    return render_template(
        "reconciliation_month.html", active="documents", all_properties=get_properties(conn), active_property=None,
        prop=prop, ym=ym, month_label=rc.month_label(ym), existing=existing, uploaded=uploaded, status=status, current=current, after=after,
        verdict=rc.verdict(existing, uploaded), detail=rc.uploaded_detail(conn, property_id, ym),
        history=rc.describe_trail(rc.trail(conn, property_id, ym)), rows=ROWS, impact_rows=IMPACT_ROWS)


def _switch(property_id, ym, target):
    conn = db.get_conn()
    prop = get_property(conn, property_id)
    if not prop or not _valid_month(ym):
        return redirect(url_for("reconcile.index"))
    before = rc.metrics(conn, property_id, ym, src.active_source(conn, property_id, ym)[0])
    if target == src.DETAILED and request.form.get("confirm") != "yes":
        flash("Tick the box to confirm you've checked the uploaded data covers this whole month. Nothing was changed.", "warning")
        return redirect(url_for("reconcile.month", property_id=property_id, ym=ym))
    old = src.set_source(conn, property_id, ym, target, note=(request.form.get("note") or "").strip() or None)
    conn.commit()
    after = rc.metrics(conn, property_id, ym, target)
    if old == target:
        flash("That month was already using this source, so nothing changed.", "info")
    else:
        verb = "now uses the detailed bookings" if target == src.DETAILED else "is back on the Excel history"
        flash(f"✓ {prop['name']}, {rc.month_label(ym)} {verb}: Gross Booking Revenue £{before['gbr']:,.0f} → £{after['gbr']:,.0f}, "
              f"booked nights {before['nights']} → {after['nights']}. Nothing was deleted; you can switch back at any time.", "success")
    return redirect(url_for("reconcile.month", property_id=property_id, ym=ym))


@bp.route("/reconciliation/<property_id>/<ym>/use-detailed", methods=["POST"])
def use_detailed(property_id, ym):
    return _switch(property_id, ym, src.DETAILED)


@bp.route("/reconciliation/<property_id>/<ym>/revert", methods=["POST"])
def revert(property_id, ym):
    return _switch(property_id, ym, src.LEGACY)


# ---------------------------------------------------------------- figure audit

def _counts(conn, property_id, ym):
    s, e = rc.bounds(ym)
    one = lambda sql, *a: conn.execute(sql, a).fetchone()
    leg = one("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM transactions WHERE property_id=? AND source IN ('excel_import','workbook') AND direction='income' AND date>=? AND date<?", property_id, s, e)
    agg = one("SELECT COUNT(*), COALESCE(SUM(julianday(check_out)-julianday(check_in)),0) FROM bookings WHERE property_id=? AND reservation_id='monthly-aggregate' AND check_in>=? AND check_in<?", property_id, s, e)
    res = lambda source: one("SELECT COUNT(*) FROM bookings WHERE property_id=? AND source=? AND status='confirmed' AND check_in<? AND check_out>?", property_id, source, e, s)[0]
    exp = one("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM transactions WHERE property_id=? AND source='upload' AND direction='expense' AND date>=? AND date<?", property_id, s, e)
    return {"legacy_tx": leg[0], "legacy_tx_amount": leg[1], "legacy_agg": agg[0], "legacy_agg_nights": int(agg[1]),
            "uploaded_res": res("upload"), "manual_res": res("manual"), "synced_res": res("ical"), "uploaded_exp": exp[0], "uploaded_exp_amount": exp[1]}


def figure_rows(conn, property_id=None, first=None, last=None):
    props = [p for p in get_properties(conn, include_overhead=False)] if not property_id else [get_property(conn, property_id)]
    all_months = set(kpis.months_with_data(conn, None)) | {ym for _p, ym in rc.detailed_months(conn)}   # incl. months only reached by a stay that began earlier
    months = [m for m in sorted(all_months) if (not first or m >= first) and (not last or m <= last)]
    rows = []
    for ym in months:
        s, e = rc.bounds(ym)
        for p in props:
            if not p:
                continue
            active, explicit = src.active_source(conn, p["id"], ym)
            c = _counts(conn, p["id"], ym)
            if not any(c.values()) and not kpis.costs(conn, p["id"], s, e):
                continue
            managed = bool(p["management_fee_pct"])
            adj = kpis.adjusted_kpi_snapshot(conn, p["id"], s, e)
            rows.append({
                "property": p["name"], "model": "managed" if managed else "operated", "ym": ym, "active": active, "explicit": explicit,
                "gbr": kpis.accommodation_revenue(conn, p["id"], s, e), "nights": kpis.booked_nights(conn, p["id"], s, e),
                "reservations": kpis.reservation_count(conn, p["id"], s, e), "occupancy": kpis.occupancy(conn, p["id"], s, e),
                "adr": kpis.adr(conn, p["id"], s, e), "revpar": kpis.revpar(conn, p["id"], s, e),
                "un_revenue": kpis.adjusted_revenue(conn, p["id"], s, e), "fee": kpis.business_income(conn, p["id"], s, e) if managed else None,
                "costs": _costs(conn, s, e, property_id=p["id"]), "profit": None if managed else adj["net_profit"], **c})
    return rows


COLS = [("property", "Property"), ("model", "Model"), ("ym", "Month"), ("active", "Active source"), ("explicit", "Chosen by a person"),
        ("gbr", "Gross Booking Revenue"), ("nights", "Booked nights"), ("reservations", "Reservations"), ("occupancy", "Occupancy"), ("adr", "ADR"), ("revpar", "RevPAR"),
        ("un_revenue", "Urban Nest Revenue"), ("fee", "Management Fee Earned"), ("costs", "Property Costs"), ("profit", "Property Profit"),
        ("legacy_tx", "Excel income rows"), ("legacy_tx_amount", "Excel income £"), ("legacy_agg", "Excel aggregate rows"), ("legacy_agg_nights", "Excel aggregate nights"),
        ("uploaded_res", "Uploaded reservations"), ("manual_res", "Manual reservations"), ("synced_res", "Synced-calendar reservations"),
        ("uploaded_exp", "Uploaded expenses"), ("uploaded_exp_amount", "Uploaded expenses £")]


@bp.route("/audit/figures")
def figures():
    conn = db.get_conn()
    pid = request.args.get("property") or None
    last = request.args.get("to") or None
    first = request.args.get("from") or None
    if not first and not last:
        months = kpis.months_with_data(conn, None)
        cur = "%04d-%02d" % kpis.current_period(conn)
        window = [m for m in months if m <= cur][-4:] + [m for m in months if m > cur][:2]
        first, last = (window[0], window[-1]) if window else (None, None)
    rows = figure_rows(conn, pid, first, last)
    if request.args.get("format") == "csv":
        buf = io.StringIO(); w = csv.writer(buf); w.writerow([label for _k, label in COLS])
        for r in rows:
            w.writerow([("" if r[k] is None else (round(r[k], 4) if isinstance(r[k], float) else r[k])) for k, _l in COLS])
        return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=figure-audit.csv"})
    return render_template("figure_audit.html", active="documents", all_properties=get_properties(conn), active_property=None,
                           rows=rows, cols=COLS, pid=pid, first=first, last=last,
                           csv_url=url_for("reconcile.figures", property=pid, format="csv", **{"from": first, "to": last}))
