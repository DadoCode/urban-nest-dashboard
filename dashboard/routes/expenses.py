import json

from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
import services.kpis as kpis
from services.audit import record, record_edits
from services.common import CATEGORIES, MONTH_NAMES, get_properties, get_property
from services.vendors import get_or_create_vendor

bp = Blueprint("expenses", __name__)


@bp.route("/expenses")
def index():
    conn = db.get_conn()
    flats = get_properties(conn, include_overhead=False)
    year, month = kpis.current_period(conn)
    start, end = kpis.month_bounds(year, month)
    # Anchored + clipped to trailing 12 months -- see the matching note in
    # routes/overview.py; an unbounded history here would cram years of
    # bars into one unreadable chart.
    anchor_ym = f"{year}-{month:02d}"
    months = [m for m in kpis.months_with_data(conn, None) if m <= anchor_ym][-12:]

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
        """SELECT v.id, v.name, SUM(t.amount) amt, COUNT(*) n
           FROM transactions t JOIN vendors v ON v.id = t.vendor_id
           WHERE t.direction='expense' AND t.category != 'reconciliation' AND t.date>=? AND t.date<?
           GROUP BY v.id ORDER BY amt DESC LIMIT 12""",
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

    # ---- ledger: filterable/searchable, independent of the "current month" above ----
    f_property = request.args.get("t_property") or ""
    f_category = request.args.get("t_category") or ""
    f_type = request.args.get("t_type") or ""  # 'opex' | 'capex' | ''
    f_vendor = request.args.get("t_vendor") or ""
    f_from = request.args.get("t_from") or ""
    f_to = request.args.get("t_to") or ""
    f_q = (request.args.get("t_q") or "").strip()

    clauses, params = ["direction='expense'"], []
    if f_property:
        clauses.append("property_id=?"); params.append(f_property)
    if f_category:
        clauses.append("category=?"); params.append(f_category)
    if f_type == "opex":
        clauses.append("capex=0")
    elif f_type == "capex":
        clauses.append("capex=1")
    if f_vendor:
        clauses.append("vendor_id=?"); params.append(int(f_vendor))
    if f_from:
        clauses.append("date>=?"); params.append(f_from + "-01")
    if f_to:
        y, m = map(int, f_to.split("-"))
        clauses.append("date<?"); params.append(kpis.month_bounds(y, m)[1])
    if f_q:
        clauses.append("(vendor LIKE ? OR description LIKE ?)")
        params += [f"%{f_q}%", f"%{f_q}%"]

    ledger = conn.execute(
        f"""SELECT t.*, p.name AS property_name FROM transactions t
            JOIN properties p ON p.id = t.property_id
            WHERE {' AND '.join(clauses)} ORDER BY date DESC, t.id DESC LIMIT 200""",
        params,
    ).fetchall()
    all_vendors = conn.execute("SELECT id, name FROM vendors ORDER BY name").fetchall()

    return render_template(
        "expenses.html", active="expenses", all_properties=get_properties(conn), active_property=None,
        current_month=f"{MONTH_NAMES[month]} {year}", rows=rows, tiles=tiles, vendor_breakdown=vendor_breakdown,
        this_month_categories=category_breakdown(start, end), all_time_categories=category_breakdown(),
        months_json=json.dumps(months), opex_json=json.dumps(opex_series), capex_json=json.dumps(capex_series),
        ledger=ledger, all_vendors=all_vendors, all_flats=flats, categories=CATEGORIES,
        f_property=f_property, f_category=f_category, f_type=f_type, f_vendor=f_vendor,
        f_from=f_from, f_to=f_to, f_q=f_q,
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
    return render_template(
        "partials/transaction_drawer.html", tx=tx, history=history, doc=doc,
        flats=get_properties(conn, include_overhead=True), categories=CATEGORIES,
    )


@bp.route("/expenses/transactions/<int:tx_id>/edit", methods=["POST"])
def edit_transaction(tx_id):
    conn = db.get_conn()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        return "<div class='card'>Transaction not found.</div>", 404
    if tx["category"] == "reconciliation":
        flash("Historical reconciliation adjustments can't be edited -- they preserve the original Excel totals.")
        return redirect(url_for("expenses.index"))

    new_property_id = request.form.get("property_id", tx["property_id"])
    if not get_property(conn, new_property_id):
        flash("That property doesn't exist -- nothing was changed.")
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
        flash("Transaction updated.")
    return redirect(url_for("expenses.index"))


@bp.route("/expenses/transactions/<int:tx_id>/delete", methods=["POST"])
def delete_transaction(tx_id):
    conn = db.get_conn()
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        return redirect(url_for("expenses.index"))
    if tx["category"] == "reconciliation":
        flash("Historical reconciliation adjustments can't be deleted -- they preserve the original Excel totals.")
        return redirect(url_for("expenses.index"))
    record(conn, "transaction", tx_id, "delete", old_value=f"{tx['vendor']} £{tx['amount']}")
    conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    conn.commit()
    flash("Transaction deleted.")
    return redirect(url_for("expenses.index"))
