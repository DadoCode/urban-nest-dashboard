import json
import re
from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
import services.ingest as ingest
import services.kpis as kpis
from services.audit import record, record_edits
from services.common import CATEGORIES, MONTH_NAMES, get_properties, get_property, pct_delta
from services.context import compare_bounds, range_params, request_context
from services.vendors import get_or_create_vendor

bp = Blueprint("expenses", __name__)

# ----------------------------------------------------------------------
# Phase 3 classification rule (see the audit note below): a transaction
# is a PROPERTY cost or a BUSINESS cost purely by which cost centre it's
# actually posted to (properties.type = 'flat' vs 'overhead') -- never
# guessed from its category. There's exactly one overhead cost centre in
# the schema today (id='general-overheads') and property_id is NOT NULL
# on every transaction, so "type='overhead'" is the complete, reliable
# signal -- no property_id IS NULL case exists to handle separately.
# ----------------------------------------------------------------------
SCOPES = ("all", "property", "business")


def _scope(args):
    s = args.get("scope", "all")
    return s if s in SCOPES else "all"


def _costs(conn, start, end, scope="all", property_id=None, capex=None):
    """Sum of real expense transactions for a scope, joined to
    properties.type so Property/Business is decided by cost-centre, not
    category. category='management_fee' is always excluded here: that
    transaction sits on a flat's own book with direction='expense'
    (money leaving the FLAT's account), which is correct for the flat's
    own P&L -- but it's the fee Urban Nest itself receives, already
    counted as Urban Nest Revenue / Management Fee Earned elsewhere.
    Counting it again here as Expenses spend would double it: once as
    income, once as a cost. Corrected after a real regression -- a first
    version of this Phase 3 pass counted it as a Property Cost, which
    put money Urban Nest earns on the same page as money it spent (see
    tests/test_expenses_management_fee.py)."""
    clauses = ["t.direction='expense'", "t.category != 'management_fee'", "t.date>=?", "t.date<?"]
    params = [start, end]
    if property_id:
        clauses.append("t.property_id=?")
        params.append(property_id)
    elif scope == "property":
        clauses.append("p.type='flat'")
    elif scope == "business":
        clauses.append("p.type='overhead'")
    if capex is not None:
        clauses.append(f"t.capex={1 if capex else 0}")
    where = " AND ".join(clauses)
    row = conn.execute(
        f"SELECT COALESCE(SUM(t.amount),0) FROM transactions t JOIN properties p ON p.id=t.property_id WHERE {where}",
        params).fetchone()
    return row[0]


def _ledger_where(ctx, start, end, args):
    """WHERE fragments for the ledger: the shared context (period,
    property) plus the ledger's own narrowing filters. A chart click
    sets t_month, which replaces the period with that single month.
    t_scope narrows to one specific property or 'business' within
    whatever the page's top-level segment already selected -- distinct
    from the global property switcher (ctx['property_id']), which the
    route only lets apply when it's set to a single specific property
    (see index()); when the global switcher is "All properties" this is
    the only property-level narrowing available."""
    month = args.get("t_month") or ""
    if month and re.match(r"^\d{4}-\d{2}$", month):
        y, m = map(int, month.split("-"))
        start, end = kpis.month_bounds(y, m)
    # category != 'management_fee' here too, for the same reason as
    # _costs() -- the ledger's own header total ("N · £X") has to match
    # the summary tiles above it, so the exclusion has to be identical
    # everywhere this page shows a total, not just in the tiles. The fee
    # transactions aren't deleted -- they're still fully visible via
    # Management Fee Earned's own drilldown and the transaction drawer,
    # just not listed as if they were company spend on this page.
    clauses = ["t.direction='expense'", "t.category != 'management_fee'", "t.date>=?", "t.date<?"]
    params = [start, end]
    if ctx["property_id"]:
        clauses.append("t.property_id=?"); params.append(ctx["property_id"])
    scope = _scope(args)
    if not ctx["property_id"]:
        if scope == "property":
            clauses.append("p.type='flat'")
        elif scope == "business":
            clauses.append("p.type='overhead'")
    t_scope = args.get("t_scope") or ""
    if t_scope == "business":
        clauses.append("p.type='overhead'")
    elif t_scope:
        clauses.append("t.property_id=?"); params.append(t_scope)
    if args.get("t_category"):
        clauses.append("t.category=?"); params.append(args["t_category"])
    if args.get("t_type") == "opex":
        clauses.append("t.capex=0")
    elif args.get("t_type") == "capex":
        clauses.append("t.capex=1")
    if args.get("t_vendor"):
        clauses.append("t.vendor_id=?"); params.append(int(args["t_vendor"]))
    if args.get("t_source"):
        clauses.append("t.source=?"); params.append(args["t_source"])
    q = (args.get("t_q") or "").strip()
    if q:
        clauses.append("(t.vendor LIKE ? OR t.description LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    return clauses, params, month, scope, t_scope


@bp.route("/expenses")
def index():
    conn = db.get_conn()
    ctx = request_context(conn)
    flats = get_properties(conn, include_overhead=False)
    pid = ctx["property_id"]
    viewing = next((p for p in get_properties(conn) if p["id"] == pid), None) if pid else None
    start, end = kpis.range_bounds(ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    cmp_bounds = compare_bounds(ctx)
    mtd = ctx["partial"] and ctx["choice"] == "this_month"
    d = lambda cur, prev, base=100: None if mtd else (pct_delta(cur, prev, min_base=base) if prev is not None else None)

    # A specific cost centre is already selected via the global context
    # bar -- the Property/Business split doesn't apply to one property,
    # so this page falls back to a single-scope view (same shape as
    # before Phase 3, just without a standalone Opex/Capex tile pair).
    scope = None if pid else _scope(request.args)

    if pid:
        cur_total = _costs(conn, start, end, property_id=pid)
        cur_opex = _costs(conn, start, end, property_id=pid, capex=False)
        cur_capex = _costs(conn, start, end, property_id=pid, capex=True)
        nights = kpis.booked_nights(conn, pid, start, end)
        if cmp_bounds:
            prev_total = _costs(conn, start=cmp_bounds[0], end=cmp_bounds[1], property_id=pid)
            prev_nights = kpis.booked_nights(conn, pid, *cmp_bounds)
        else:
            prev_total = prev_nights = None
        summary_tiles = [{
            "label": "Total costs", "value": f"£{cur_total:,.0f}",
            "sub": f"Opex £{cur_opex:,.0f} · Capex £{cur_capex:,.0f}",
            "delta": d(cur_total, prev_total),
        }, {
            "label": "Cost / booked night", "info": "Total recorded costs for this property divided by its booked nights in the selected period.",
            "value": f"£{(cur_total / nights):,.0f}" if nights else "£0",
            "delta": d(cur_total / nights if nights else 0, prev_total / prev_nights if prev_nights else None, base=5),
        }]
        property_total = business_total = None
    else:
        property_total = _costs(conn, start, end, scope="property")
        business_total = _costs(conn, start, end, scope="business")
        total = property_total + business_total
        opex = _costs(conn, start, end, scope=scope, capex=False)
        capex = _costs(conn, start, end, scope=scope, capex=True)
        headline = {"all": total, "property": property_total, "business": business_total}[scope]
        nights = kpis.booked_nights(conn, None, start, end)

        def prev_of(fn):
            return fn(*cmp_bounds) if cmp_bounds else None

        summary_tiles = []
        if scope == "all":
            summary_tiles.append({"label": "Property Costs", "value": f"£{property_total:,.0f}",
                                   "delta": d(property_total, prev_of(lambda a, b: _costs(conn, a, b, scope="property")))})
            summary_tiles.append({"label": "Business Costs", "value": f"£{business_total:,.0f}",
                                   "delta": d(business_total, prev_of(lambda a, b: _costs(conn, a, b, scope="business")))})
        headline_label = {"all": "Total Recorded Costs", "property": "Property Costs", "business": "Business Costs"}[scope]
        summary_tiles.append({
            "label": headline_label, "value": f"£{headline:,.0f}",
            "sub": f"Opex £{opex:,.0f} · Capex £{capex:,.0f}",
            "delta": d(headline, prev_of(lambda a, b: _costs(conn, a, b, scope=scope))),
        })
        # Cost/booked night isn't a meaningful number for Business Costs
        # alone (company overhead has no "booked nights" of its own) --
        # left out of that view rather than forced in. See routes/
        # expenses.py's index() docstring / the Phase 3 report for the
        # reasoning; recorded here so it isn't silently redefined later.
        if scope != "business":
            numerator = total if scope == "all" else property_total
            info = ("Total recorded costs (property + business) divided by booked nights in the selected period."
                    if scope == "all" else
                    "Property costs divided by booked nights in the selected period.")
            summary_tiles.append({
                "label": "Cost / booked night", "info": info,
                "value": f"£{(numerator / nights):,.0f}" if nights else "£0",
                "delta": d(numerator / nights if nights else 0,
                           (prev_of(lambda a, b: _costs(conn, a, b, scope=scope)) / kpis.booked_nights(conn, None, *cmp_bounds))
                           if cmp_bounds and kpis.booked_nights(conn, None, *cmp_bounds) else None, base=5),
            })

    # Anchored + clipped to trailing 12 months, same rule as every other
    # trend chart in the app.
    anchor_ym = f"{ctx['end_year']}-{ctx['end_month']:02d}"
    months = [m for m in kpis.months_with_data(conn, pid) if m <= anchor_ym][-12:]
    if pid:
        chart_series = {"Costs": []}
        for ym in months:
            y, m = map(int, ym.split("-"))
            s, e = kpis.month_bounds(y, m)
            chart_series["Costs"].append(_costs(conn, s, e, property_id=pid))
    else:
        # Property vs Business, not Opex vs Capex -- that's the split
        # this page is actually organised around now (item 12). A
        # single property view above keeps one plain Costs series:
        # splitting Property/Business for one already-single-scope
        # property would just relabel the same bar, not add information.
        chart_series = {"Property Costs": [], "Business Costs": []}
        for ym in months:
            y, m = map(int, ym.split("-"))
            s, e = kpis.month_bounds(y, m)
            chart_series["Property Costs"].append(_costs(conn, s, e, scope="property"))
            chart_series["Business Costs"].append(_costs(conn, s, e, scope="business"))

    # ---- Property Costs section: by-property table + category breakdown ----
    property_rows = []
    property_categories = []
    if not pid and scope in ("all", "property"):
        for p in flats:
            opex = _costs(conn, start, end, property_id=p["id"], capex=False)
            capex_amt = _costs(conn, start, end, property_id=p["id"], capex=True)
            property_rows.append({"id": p["id"], "name": p["name"], "opex": opex, "capex": capex_amt, "total": opex + capex_amt})
        property_rows.sort(key=lambda r: r["total"], reverse=True)
        property_categories = conn.execute(
            """SELECT t.category, SUM(t.amount) amt, COUNT(*) n FROM transactions t JOIN properties p ON p.id=t.property_id
               WHERE t.direction='expense' AND t.category != 'management_fee' AND p.type='flat' AND t.date>=? AND t.date<?
               GROUP BY t.category ORDER BY amt DESC""",
            (start, end)).fetchall()

    # ---- Business Costs section: category breakdown only (no "by property" -- there is no property) ----
    business_categories = []
    if not pid and scope in ("all", "business"):
        business_categories = conn.execute(
            """SELECT t.category, SUM(t.amount) amt, COUNT(*) n FROM transactions t JOIN properties p ON p.id=t.property_id
               WHERE t.direction='expense' AND t.category != 'management_fee' AND p.type='overhead' AND t.date>=? AND t.date<?
               GROUP BY t.category ORDER BY amt DESC""",
            (start, end)).fetchall()

    # ---- vendors, respecting the current scope ----
    vendor_scope_clause, vendor_scope_params = "", []
    if pid:
        vendor_scope_clause, vendor_scope_params = "AND t.property_id=?", [pid]
    elif scope == "property":
        vendor_scope_clause = "AND p.type='flat'"
    elif scope == "business":
        vendor_scope_clause = "AND p.type='overhead'"
    # LEFT JOIN, not JOIN: a transaction can carry a raw vendor name
    # (t.vendor) without yet having a resolved vendor_id -- an INNER
    # JOIN here silently dropped every such row, which is exactly why
    # this table showed "No vendors" on data that plainly had vendor
    # names in its own ledger (confirmed against the demo: vendor_id was
    # NULL on every transaction there, purely a seed-data gap, but the
    # query itself needed to not depend on vendor_id being populated to
    # begin with). Grouped by the resolved name so id-linked and
    # not-yet-linked spellings of the same vendor still merge together
    # when they happen to match exactly; v.id (kept for the click-
    # through filter) is NULL for an unlinked row, so the template falls
    # back to a text-search link for those.
    vendors = conn.execute(
        f"""SELECT v.id, COALESCE(v.name, t.vendor) AS name, SUM(t.amount) amt, COUNT(*) n FROM transactions t
            LEFT JOIN vendors v ON v.id = t.vendor_id JOIN properties p ON p.id = t.property_id
            WHERE t.direction='expense' AND t.category NOT IN ('reconciliation', 'management_fee')
              AND COALESCE(v.name, t.vendor) IS NOT NULL AND COALESCE(v.name, t.vendor) != ''
              AND t.date>=? AND t.date<? {vendor_scope_clause}
            GROUP BY COALESCE(v.name, t.vendor) ORDER BY amt DESC LIMIT 10""",
        (start, end, *vendor_scope_params)).fetchall()
    # Salary, rent and similar recurring costs usually carry no vendor
    # name at all -- real, not a bug, but a first-time viewer comparing
    # this total against the tile above it would otherwise wonder where
    # the rest went. Same WHERE condition as the ranking above, just
    # inverted, so the two numbers always add up to the same total this
    # page already shows elsewhere.
    vendorless_total = conn.execute(
        f"""SELECT COALESCE(SUM(t.amount),0) FROM transactions t
            LEFT JOIN vendors v ON v.id = t.vendor_id JOIN properties p ON p.id = t.property_id
            WHERE t.direction='expense' AND t.category NOT IN ('reconciliation', 'management_fee')
              AND (COALESCE(v.name, t.vendor) IS NULL OR COALESCE(v.name, t.vendor) = '')
              AND t.date>=? AND t.date<? {vendor_scope_clause}""",
        (start, end, *vendor_scope_params)).fetchone()[0]

    # ---- ledger, on this page, driven by the shared context + its own filters ----
    clauses, params, f_month, f_scope, f_t_scope = _ledger_where(ctx, start, end, request.args)
    where = " AND ".join(clauses)
    # Default: newest first. Date and Amount are the two sortable columns;
    # id is the stable tiebreaker so equal dates/amounts never shuffle.
    t_sort = request.args.get("t_sort") if request.args.get("t_sort") in ("date", "amount") else "date"
    t_dir = request.args.get("t_dir") if request.args.get("t_dir") in ("asc", "desc") else "desc"
    order = f"t.{t_sort} {t_dir.upper()}, t.id {t_dir.upper()}"
    ledger = conn.execute(
        f"""SELECT t.*, p.name AS property_name, p.type AS property_type FROM transactions t
            JOIN properties p ON p.id = t.property_id
            WHERE {where} ORDER BY {order} LIMIT 200""", params).fetchall()
    ledger_total = conn.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(t.amount),0) amt FROM transactions t JOIN properties p ON p.id=t.property_id WHERE {where}",
        params).fetchone()

    f = {k: request.args.get(k) or "" for k in ("t_category", "t_type", "t_vendor", "t_q", "t_scope", "t_source")}
    f["t_month"] = f_month
    chips = []
    base = range_params(ctx)
    def chip(label, drop):
        keep = {k: v for k, v in f.items() if v and k != drop}
        if (t_sort, t_dir) != ("date", "desc"):
            keep.update(t_sort=t_sort, t_dir=t_dir)
        chips.append({"label": label, "href": url_for("expenses.index", **{**base, **({"scope": scope} if scope else {}), **keep}) + "#ledger"})
    if f_month:
        y, m = map(int, f_month.split("-")); chip(f"{MONTH_NAMES[m]} {y}", "t_month")
    if f["t_scope"]:
        sname = "Business" if f["t_scope"] == "business" else next((p["name"] for p in flats if p["id"] == f["t_scope"]), f["t_scope"])
        chip(sname, "t_scope")
    if f["t_type"]:
        chip(f["t_type"].capitalize(), "t_type")
    if f["t_category"]:
        chip(f["t_category"].replace("_", " ").title(), "t_category")
    if f["t_vendor"]:
        vname = conn.execute("SELECT name FROM vendors WHERE id=?", (f["t_vendor"],)).fetchone()
        chip(vname["name"] if vname else "Vendor", "t_vendor")
    if f["t_source"]:
        chip(f["t_source"].replace("_", " ").title(), "t_source")
    if f["t_q"]:
        chip(f'"{f["t_q"]}"', "t_q")

    def sort_href(column):
        """Header link: sort by this column; clicking the active one flips
        direction. Keeps the period, scope and every filter."""
        direction = ("asc" if t_dir == "desc" else "desc") if t_sort == column else "desc"
        keep = {k: v for k, v in f.items() if v}
        return url_for("expenses.index", **{**base, **({"scope": scope} if scope else {}), **keep,
                                           "t_sort": column, "t_dir": direction}) + "#ledger"

    sources = [r["source"] for r in conn.execute("SELECT DISTINCT source FROM transactions WHERE source IS NOT NULL ORDER BY source")]

    return render_template(
        "expenses.html", active="expenses", all_properties=get_properties(conn), active_property=None,
        context_bar=True, ctx=ctx, viewing=viewing, scope=scope, flats=flats,
        summary_tiles=summary_tiles, property_rows=property_rows,
        property_categories=property_categories, business_categories=business_categories,
        vendor_rows=vendors, vendorless_total=vendorless_total, ledger=ledger, ledger_total=ledger_total,
        f=f, chips=chips, t_sort=t_sort, t_dir=t_dir, sort_href=sort_href, sources=sources, ledger_base=urlencode({**base, **({"scope": scope} if scope else {})}), base_params=base,
        all_vendors=conn.execute("SELECT id, name FROM vendors ORDER BY name").fetchall(),
        categories=CATEGORIES,
        months_json=json.dumps(months), chart_series_json=json.dumps(chart_series),
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
        prov=ingest.provenance(conn, tx["document_id"], line),
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
        flash("✓ Transaction updated.", "success")
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
    flash("✓ Transaction deleted.", "success")
    return redirect(url_for("expenses.index"))
