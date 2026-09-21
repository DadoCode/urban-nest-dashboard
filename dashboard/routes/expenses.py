import json
import re
from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
import services.kpis as kpis
from services.audit import record, record_edits
from services.common import CATEGORIES, MONTH_NAMES, get_properties, get_property, pct_delta
from services.context import compare_bounds, range_params, request_context
from services.vendors import get_or_create_vendor

bp = Blueprint("expenses", __name__)


def _ledger_where(ctx, start, end, args):
    """WHERE fragments for the ledger: the shared context (period, property)
    plus the ledger's own narrowing filters. A chart click sets t_month,
    which replaces the period with that single month."""
    month = args.get("t_month") or ""
    if month and re.match(r"^\d{4}-\d{2}$", month):
        y, m = map(int, month.split("-"))
        start, end = kpis.month_bounds(y, m)
    clauses, params = ["t.direction='expense'", "t.date>=?", "t.date<?"], [start, end]
    if ctx["property_id"]:
        clauses.append("t.property_id=?"); params.append(ctx["property_id"])
    if args.get("t_category"):
        clauses.append("t.category=?"); params.append(args["t_category"])
    if args.get("t_type") == "opex":
        clauses.append("t.capex=0")
    elif args.get("t_type") == "capex":
        clauses.append("t.capex=1")
    if args.get("t_vendor"):
        clauses.append("t.vendor_id=?"); params.append(int(args["t_vendor"]))
    q = (args.get("t_q") or "").strip()
    if q:
        clauses.append("(t.vendor LIKE ? OR t.description LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    return clauses, params, month


@bp.route("/expenses")
def index():
    conn = db.get_conn()
    ctx = request_context(conn)
    flats = get_properties(conn, include_overhead=False)
    pid = ctx["property_id"]
    viewing = next((p for p in get_properties(conn) if p["id"] == pid), None) if pid else None
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    cmp_bounds = compare_bounds(ctx)
    scope, scope_params = ("AND property_id=?", (pid,)) if pid else ("", ())

    # Anchored + clipped to trailing 12 months ending at the selected period.
    anchor_ym = f"{ctx['end_year']}-{ctx['end_month']:02d}"
    months = [m for m in kpis.months_with_data(conn, pid) if m <= anchor_ym][-12:]
    opex_series, capex_series = [], []
    for ym in months:
        y, m = map(int, ym.split("-"))
        s, e = kpis.month_bounds(y, m)
        opex_series.append(kpis.costs(conn, pid, s, e, capex=False))
        capex_series.append(kpis.costs(conn, pid, s, e, capex=True))

    def tally(a, b):
        total_opex, total_capex = kpis.costs(conn, pid, a, b, capex=False), kpis.costs(conn, pid, a, b, capex=True)
        cleaning = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE direction='expense' AND category='cleaning' AND date>=? AND date<? {scope}",
            (a, b, *scope_params)).fetchone()[0]
        nights = kpis.booked_nights(conn, pid, a, b)
        return {"total": total_opex + total_capex, "opex": total_opex, "capex": total_capex, "cleaning": cleaning,
                "per_night": (total_opex + total_capex) / nights if nights else 0}

    cur = tally(start, end)
    prev = tally(*cmp_bounds) if cmp_bounds else None
    d = lambda key, base=100: pct_delta(cur[key], prev[key], min_base=base) if prev else None
    tiles = [
        {"label": "Total costs", "value": f"£{cur['total']:,.0f}", "delta": d("total"), "href": None},
        {"label": "Opex", "value": f"£{cur['opex']:,.0f}", "delta": d("opex"), "href": {"t_type": "opex"}},
        {"label": "Capex", "value": f"£{cur['capex']:,.0f}", "delta": d("capex", 200), "href": {"t_type": "capex"}},
        {"label": "Cleaning", "value": f"£{cur['cleaning']:,.0f}", "delta": d("cleaning", 50), "href": {"t_category": "cleaning"}},
        {"label": "Cost / booked night", "value": f"£{cur['per_night']:,.0f}", "delta": d("per_night", 5), "href": None},
    ]

    property_rows = []
    if not pid:
        for p in flats:
            opex = kpis.costs(conn, p["id"], start, end, capex=False)
            capex = kpis.costs(conn, p["id"], start, end, capex=True)
            property_rows.append({"id": p["id"], "name": p["name"], "opex": opex, "capex": capex, "total": opex + capex})
        property_rows.sort(key=lambda r: r["total"], reverse=True)
        overhead = next((p for p in get_properties(conn) if p["type"] == "overhead"), None)
        if overhead:
            oc = kpis.costs(conn, overhead["id"], start, end)
            if oc:
                property_rows.append({"id": overhead["id"], "name": overhead["name"], "opex": oc, "capex": 0, "total": oc, "overhead": True})

    categories = conn.execute(
        f"""SELECT category, SUM(amount) amt, COUNT(*) n FROM transactions
            WHERE direction='expense' AND date>=? AND date<? {scope} GROUP BY category ORDER BY amt DESC""",
        (start, end, *scope_params)).fetchall()
    vendors = conn.execute(
        f"""SELECT v.id, v.name, SUM(t.amount) amt, COUNT(*) n FROM transactions t JOIN vendors v ON v.id = t.vendor_id
            WHERE t.direction='expense' AND t.category != 'reconciliation' AND t.date>=? AND t.date<? {scope.replace('property_id', 't.property_id')}
            GROUP BY v.id ORDER BY amt DESC LIMIT 10""",
        (start, end, *scope_params)).fetchall()

    # ---- ledger, on this page, driven by the shared context + its own filters ----
    clauses, params, f_month = _ledger_where(ctx, start, end, request.args)
    where = " AND ".join(clauses)
    ledger = conn.execute(
        f"""SELECT t.*, p.name AS property_name FROM transactions t JOIN properties p ON p.id = t.property_id
            WHERE {where} ORDER BY t.date DESC, t.id DESC LIMIT 200""", params).fetchall()
    ledger_total = conn.execute(f"SELECT COUNT(*) n, COALESCE(SUM(t.amount),0) amt FROM transactions t WHERE {where}", params).fetchone()

    f = {k: request.args.get(k) or "" for k in ("t_category", "t_type", "t_vendor", "t_q")}
    f["t_month"] = f_month
    chips = []
    base = range_params(ctx)
    def chip(label, drop):
        keep = {k: v for k, v in f.items() if v and k != drop}
        chips.append({"label": label, "href": url_for("expenses.index", **{**base, **keep}) + "#ledger"})
    if f_month:
        y, m = map(int, f_month.split("-")); chip(f"{MONTH_NAMES[m]} {y}", "t_month")
    if f["t_type"]:
        chip(f["t_type"].capitalize(), "t_type")
    if f["t_category"]:
        chip(f["t_category"].replace("_", " ").capitalize(), "t_category")
    if f["t_vendor"]:
        vname = conn.execute("SELECT name FROM vendors WHERE id=?", (f["t_vendor"],)).fetchone()
        chip(vname["name"] if vname else "Vendor", "t_vendor")
    if f["t_q"]:
        chip(f'"{f["t_q"]}"', "t_q")

    return render_template(
        "expenses.html", active="expenses", all_properties=get_properties(conn), active_property=None,
        context_bar=True, ctx=ctx, viewing=viewing, tiles=tiles, property_rows=property_rows,
        categories_rows=categories, vendor_rows=vendors, ledger=ledger, ledger_total=ledger_total,
        f=f, chips=chips, ledger_base=urlencode(base), base_params=base,
        all_vendors=conn.execute("SELECT id, name FROM vendors ORDER BY name").fetchall(), categories=CATEGORIES,
        months_json=json.dumps(months), opex_json=json.dumps(opex_series), capex_json=json.dumps(capex_series),
        compare_label=ctx["compare_display"] or "",
    )


@bp.route("/expenses/transactions/<int:tx_id>")
def transaction_drawer(tx_id):
    conn = db.get_conn()
    tx = conn.execute(
        """SELECT t.*, p.name AS property_name FROM transactions t
           JOIN properties p ON p.id = t.property_id WHERE t.id=?""",
        (tx_id,),
    ).fetchone()
    if not tx:
        return "<div class='card'>Transaction not found.</div>", 404
    history = conn.execute(
        "SELECT * FROM audit_log WHERE entity_type='transaction' AND entity_id=? ORDER BY timestamp DESC LIMIT 10",
        (tx_id,),
    ).fetchall()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (tx["document_id"],)).fetchone() if tx["document_id"] else None
    line = None
    if doc:
        line = conn.execute("SELECT * FROM document_items WHERE document_id=? AND duplicate_of=? LIMIT 1", (doc["id"], tx_id)).fetchone() \
            or conn.execute("SELECT * FROM document_items WHERE document_id=? AND ABS(amount-?)<0.01 AND include=1 LIMIT 1", (doc["id"], tx["amount"])).fetchone()
    return render_template(
        "partials/transaction_drawer.html", tx=tx, history=history, doc=doc, line=line,
        flats=get_properties(conn, include_overhead=True), categories=CATEGORIES,
    )


@bp.route("/expenses/transactions/<int:tx_id>/edit", methods=["POST"])
def edit_transaction(tx_id):
    conn = db.get_conn()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        return "<div class='card'>Transaction not found.</div>", 404
    if tx["category"] == "reconciliation":
        flash("Historical adjustments can't be edited: they keep the historical totals matching the original Excel accounts.", "warning")
        return redirect(url_for("expenses.index"))

    new_property_id = request.form.get("property_id", tx["property_id"])
    if not get_property(conn, new_property_id):
        flash("That property doesn't exist, so nothing was changed. Choose one from the list.", "error")
        return redirect(url_for("expenses.index"))

    vendor_name = request.form.get("vendor", "").strip()
    vendor_id = get_or_create_vendor(conn, vendor_name)
    new_values = {
        "property_id": new_property_id,
        "vendor": vendor_name,
        "description": request.form.get("description", tx["description"] or ""),
        "amount": abs(float(request.form.get("amount") or tx["amount"])),
        "category": request.form.get("category", tx["category"]),
        "capex": 1 if request.form.get("capex") == "on" else 0,
    }
    changed = record_edits(conn, "transaction", tx_id, tx, new_values)
    if changed:
        conn.execute(
            """UPDATE transactions SET property_id=?, vendor=?, vendor_id=?, description=?,
                 amount=?, category=?, capex=?, edited_at=datetime('now') WHERE id=?""",
            (new_values["property_id"], vendor_name, vendor_id, new_values["description"],
             new_values["amount"], new_values["category"], new_values["capex"], tx_id),
        )
        conn.commit()
        flash("\u2713 Transaction updated.", "success")
    return redirect(url_for("expenses.index"))


@bp.route("/expenses/transactions/<int:tx_id>/delete", methods=["POST"])
def delete_transaction(tx_id):
    conn = db.get_conn()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        return redirect(url_for("expenses.index"))
    if tx["category"] == "reconciliation":
        flash("Historical adjustments can't be deleted: they keep the historical totals matching the original Excel accounts.", "warning")
        return redirect(url_for("expenses.index"))
    record(conn, "transaction", tx_id, "delete", old_value=f"{tx['vendor']} £{tx['amount']}")
    conn.execute("UPDATE document_items SET duplicate_of=NULL WHERE duplicate_of=?", (tx_id,))
    conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    conn.commit()
    flash("\u2713 Transaction deleted.", "success")
    return redirect(url_for("expenses.index"))
