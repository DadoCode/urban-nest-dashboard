"""Turn the parsed workbook into a per-month import PLAN, without writing anything.

For each mapped property (and the business cost centre) and the selected month:
  * the ledger rows the workbook says should exist ("desired"),
  * a row-level diff against what the dashboard holds from earlier workbook/Excel
    imports (NEW / CHANGED / REMOVED / UNCHANGED),
  * reconciliation checks: workbook control totals vs the detail that would be imported,
  * the CURRENT dashboard figures, and the figures AFTER the import, computed by
    running the real KPI code against a rolled-back copy of the change.

Workbook month-summary fields (Net Profit, Operating Profit, Income, Total Costs,
Opex, Capex, Occupancy, Days Booked) are CONTROL TOTALS: they validate the detail
and are never written as ledger rows. Days Booked is the one authoritative
operating figure; it becomes a monthly-aggregate booking, exactly like the history.
"""
import calendar
import contextlib

from services import kpis, sources as src
from . import config as C
from .reader import ym as make_ym

BUSINESS_ID = "general-overheads"
AGG = src.AGGREGATE_SOURCES
_AGG_IN = "('" + "','".join(AGG) + "')"


# ------------------------------------------------------------------ dates
def bounds(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return kpis.month_bounds(y, m)


def days_in(ym):
    return calendar.monthrange(int(ym[:4]), int(ym[5:7]))[1]


def _r2(x):
    return round(float(x or 0), 2)


def _norm(text):
    return " ".join((text or "").lower().split())


# ------------------------------------------------------------ desired rows
def _tx(pid, ym, direction, description, amount, category, capex=0, vendor=None, ref=None):
    return {"property_id": pid, "date": f"{ym}-01", "vendor": vendor or None, "description": description,
            "amount": round(float(amount), 4), "direction": direction, "category": category,
            "capex": int(capex), "source": "workbook", "source_ref": ref}


def desired_property(parsed, code, ym):
    """The ledger rows and operating days the workbook asks for, for one property-month."""
    pid = C.PROPERTY_SHEETS[code][0]
    prop = parsed["properties"][code]
    rows, notes = [], []
    detail = (parsed.get("breakdown", {}).get(code) or {}).get(ym)
    has_detail = bool(detail and detail["items"])
    for item in prop["income"].get(ym, {}).get("items", []):
        rows.append(_tx(pid, ym, "income", item["label"], item["amount"], "booking_income", 0, None, item["ref"]))
    for block, capex in (("opex", 0), ("capex", 1)):
        for item in prop[block].get(ym, {}).get("items", []):
            if has_detail and item["label"].strip().lower() == "purchases":
                notes.append(f"'{item['label']}' ({item['amount']:.2f}) replaced by {len(detail['items'])} itemised breakdown rows")
                continue
            rows.append(_tx(pid, ym, "expense", item["label"], item["amount"], C.categorise(item["label"]), capex, None, item["ref"]))
    if has_detail:
        for item in detail["items"]:
            rows.append(_tx(pid, ym, "expense", item["description"] or item["vendor"] or "purchase", item["amount"],
                            "purchase", int(item["capex"]), item["vendor"], item["ref"]))
    summary = prop["summary"].get(ym, {})
    days, derived = summary.get("days"), False
    if days is None and summary.get("occupancy"):
        days, derived = round(summary["occupancy"] * days_in(ym)), True
        notes.append(f"Days Booked blank; derived {days} from occupancy")
    return {"rows": rows, "days": days, "days_derived": derived, "notes": notes, "has_detail": has_detail}


def desired_business(parsed, ym):
    month = parsed["main"]["months"].get(ym)
    if not month:
        return {"rows": [], "notes": []}
    rows = []
    for item in month["items"]:
        rows.append(_tx(BUSINESS_ID, ym, "expense", item["label"], item["amount"], C.categorise(item["label"], business=True), 0, None, item["ref"]))
    for item in month["salaries"]:
        label = item["label"]
        desc = label if "salary" in label.lower() else f"{label} salary"
        rows.append(_tx(BUSINESS_ID, ym, "expense", desc, item["amount"], "salary", 0, None, item["ref"]))
    return {"rows": rows, "notes": []}


# -------------------------------------------------------------- current rows
def current_rows(conn, pid, ym):
    """Aggregate-layer rows (earlier workbook / Excel imports) for the property-month, and its aggregate nights."""
    s, e = bounds(ym)
    tx = [dict(r) for r in conn.execute(
        f"""SELECT * FROM transactions WHERE property_id=? AND date>=? AND date<? AND source IN {_AGG_IN} ORDER BY id""", (pid, s, e))]
    agg = [dict(r) for r in conn.execute(
        f"""SELECT * FROM bookings WHERE property_id=? AND reservation_id='monthly-aggregate' AND check_in>=? AND check_in<?
            AND source IN {_AGG_IN} ORDER BY id""", (pid, s, e))]
    return tx, agg


def agg_nights(agg_rows):
    import datetime
    return sum((datetime.date.fromisoformat(r["check_out"]) - datetime.date.fromisoformat(r["check_in"])).days
               for r in agg_rows if r["status"] == "confirmed")


def diff_rows(current, desired):
    """Pair current rows with desired rows. -> list of
    {"status": NEW|CHANGED|REMOVED|UNCHANGED, "old": row|None, "new": row|None, "note": str}.
    Pass 1 same description+amount; pass 2 same description, different amount (CHANGED);
    pass 3 same direction+capex+amount under a different label (UNCHANGED, label kept)."""
    out = []
    cur = list(current)
    want = list(desired)

    def kk(r):
        return (r["direction"], int(r["capex"]), _norm(r["description"]))

    def take(predicate_new, predicate_old, status, note=""):
        nonlocal cur, want
        for n in list(want):
            for o in cur:
                if predicate_old(o, n):
                    out.append({"status": status, "old": o, "new": n, "note": note})
                    want.remove(n)
                    cur.remove(o)
                    break

    take(None, lambda o, n: kk(o) == kk(n) and _r2(o["amount"]) == _r2(n["amount"]), "UNCHANGED")
    take(None, lambda o, n: kk(o) == kk(n), "CHANGED")
    take(None, lambda o, n: (o["direction"], int(o["capex"]), _r2(o["amount"])) == (n["direction"], int(n["capex"]), _r2(n["amount"])),
         "UNCHANGED", "label differs; existing row kept")
    out += [{"status": "REMOVED", "old": o, "new": None, "note": ""} for o in cur]
    out += [{"status": "NEW", "old": None, "new": n, "note": ""} for n in want]
    order = {"NEW": 0, "CHANGED": 1, "REMOVED": 2, "UNCHANGED": 3}
    out.sort(key=lambda d: (order[d["status"]], _norm((d["new"] or d["old"])["description"])))
    return out


# -------------------------------------------------------------- dashboard view
def dashboard_view(conn, pid, ym):
    """The figures the dashboard itself shows for a property-month, from the real KPI code."""
    s, e = bounds(ym)
    prop = conn.execute("SELECT type, management_fee_pct FROM properties WHERE id=?", (pid,)).fetchone()
    expense = lambda cond="": conn.execute(
        f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE property_id=? AND direction='expense' AND date>=? AND date<? {cond}",
        (pid, s, e)).fetchone()[0]
    if prop and prop["type"] == "overhead":
        return {"model": "business", "business_costs": _r2(expense()), "revenue": 0.0, "property_costs": 0.0, "management_fee": 0.0,
                "days": 0, "occupancy": 0.0, "profit": 0.0, "total_expenses": _r2(expense())}
    managed = bool(prop and prop["management_fee_pct"])
    snap = kpis.adjusted_kpi_snapshot(conn, pid, s, e)
    return {
        "model": "managed" if managed else "operated",
        "revenue": _r2(kpis.revenue(conn, pid, s, e)),                         # booking value the property took (net of platform fees)
        "total_expenses": _r2(expense()),                                      # every expense incl. the management fee
        "property_costs": _r2(expense("AND category != 'management_fee'")),    # the Expenses page rule: fee excluded
        "management_fee": _r2(kpis.business_income(conn, pid, s, e)) if managed else 0.0,
        "days": int(kpis.booked_nights(conn, pid, s, e)),
        "occupancy": round(kpis.occupancy(conn, pid, s, e), 4),
        "profit": _r2(snap["net_profit"]),                                     # Property Profit (operated) / Management Fee Earned (managed)
        "urban_nest_revenue": _r2(snap["revenue"]),
    }


@contextlib.contextmanager
def simulate(conn):
    """Everything written inside is rolled back -- used to compute 'after' with the real KPI code."""
    conn.execute("SAVEPOINT wb_sim")
    try:
        yield
    finally:
        conn.execute("ROLLBACK TO wb_sim")
        conn.execute("RELEASE wb_sim")


# ------------------------------------------------------------ reconciliation
def _check(metric, wb, imp, tol=C.TOLERANCE, note="", fmt="money", control=True):
    if wb is None:
        status = "PASS" if (imp in (0, 0.0, None)) else "REVIEW"
        note = note or ("workbook control total is blank" if status == "REVIEW" else "blank, nothing imported")
        return {"metric": metric, "workbook": None, "imported": imp, "diff": None, "status": status, "note": note, "fmt": fmt}
    diff = round(imp - wb, 4) if imp is not None else None
    ok = diff is not None and abs(diff) <= tol
    return {"metric": metric, "workbook": wb, "imported": imp, "diff": diff, "status": "PASS" if ok else "REVIEW", "note": note, "fmt": fmt}


def reconcile_property(parsed, code, ym, want, conn=None):
    prop = parsed["properties"][code]
    summary = prop["summary"].get(ym, {})
    rows = want["rows"]
    inc = sum(r["amount"] for r in rows if r["direction"] == "income")
    opex = sum(r["amount"] for r in rows if r["direction"] == "expense" and not r["capex"])
    capex = sum(r["amount"] for r in rows if r["direction"] == "expense" and r["capex"])
    days = want["days"] or 0
    checks = [
        _check("Income", summary.get("income"), round(inc, 4)),
        _check("Opex", summary.get("opex"), round(opex, 4)),
        _check("Capex", summary.get("capex"), round(capex, 4)),
        _check("Total costs", None if summary.get("total_costs") is None else -summary["total_costs"], round(opex + capex, 4)),
        _check("Net profit (control)", summary.get("net"), round(inc - opex - capex, 4), note="control only, not imported"),
        _check("Operating profit (control)", summary.get("operating"), round(inc - opex, 4), note="control only, not imported"),
        _check("Days booked", summary.get("days"), days, tol=0, fmt="int", note=("derived from occupancy" if want["days_derived"] else "")),
        _check("Occupancy", summary.get("occupancy"), round(days / days_in(ym), 4), tol=C.OCCUPANCY_TOLERANCE, fmt="pct",
               note="workbook occupancy vs days / days in month"),
    ]
    # the workbook's own block totals vs the rows read from the blocks
    worst, details = 0.0, []
    for block, mine in (("opex", sum(i["amount"] for i in prop["opex"].get(ym, {}).get("items", []))),
                        ("capex", sum(i["amount"] for i in prop["capex"].get(ym, {}).get("items", []))),
                        ("income", sum(i["amount"] for i in prop["income"].get(ym, {}).get("items", [])))):
        total = prop[block].get(ym, {}).get("total")
        if total is not None and abs(total - mine) > C.TOLERANCE:
            worst = max(worst, abs(total - mine))
            details.append(f"{block}: block total {total:.2f} vs rows {mine:.2f}")
    checks.append({"metric": "Detail rows vs block totals", "workbook": None, "imported": None, "diff": round(worst, 4) if worst else 0.0,
                   "status": "REVIEW" if worst else "PASS", "note": "; ".join(details), "fmt": "money"})
    # item-level purchases detail vs the 'Purchases' lump the property sheet links to it
    detail = (parsed.get("breakdown", {}).get(code) or {}).get(ym)
    if detail and detail["items"]:
        lump_o = sum(i["amount"] for i in prop["opex"].get(ym, {}).get("items", []) if i["label"].strip().lower() == "purchases")
        lump_c = sum(i["amount"] for i in prop["capex"].get(ym, {}).get("items", []) if i["label"].strip().lower() == "purchases")
        d_o = sum(i["amount"] for i in detail["items"] if not i["capex"])
        d_c = sum(i["amount"] for i in detail["items"] if i["capex"])
        gap = max(abs(lump_o - d_o), abs(lump_c - d_c))
        checks.append({"metric": "Purchases detail vs sheet lump", "workbook": round(lump_o + lump_c, 4), "imported": round(d_o + d_c, 4),
                       "diff": round(gap, 4), "status": "PASS" if gap <= C.TOLERANCE else "REVIEW",
                       "note": f"{len(detail['items'])} breakdown rows replace the lump", "fmt": "money"})
        sub_gap = max(abs((detail["opex"] or 0) - d_o), abs((detail["capex"] or 0) - d_c))
        if sub_gap > C.TOLERANCE:
            checks.append({"metric": "Breakdown subtotals vs rows", "workbook": None, "imported": None, "diff": round(sub_gap, 4),
                           "status": "REVIEW", "note": "OPEX/CAPEX subtotal differs from the rows its own formula counts", "fmt": "money"})
    # management fee: Main Page figure vs the fee that will be on the books
    fee_wb = parsed["main"]["months"].get(ym, {}).get("fees", {}).get(code)
    if fee_wb is not None and conn is not None:
        pid = C.PROPERTY_SHEETS[code][0]
        pct = (conn.execute("SELECT management_fee_pct FROM properties WHERE id=?", (pid,)).fetchone() or [None])[0]
        recorded = sum(r["amount"] for r in rows if r["category"] == "management_fee")
        if recorded:
            checks.append(_check("Management fee", fee_wb, round(recorded, 4), note="Main Page vs fee row on the property sheet"))
        elif pct:
            checks.append(_check("Management fee", fee_wb, round(inc * pct / 100, 4), note=f"Main Page vs dashboard estimate at {pct:g}% of income (no fee row on the sheet)"))
        else:
            checks.append({"metric": "Management fee", "workbook": fee_wb, "imported": 0.0, "diff": -fee_wb, "status": "REVIEW",
                           "note": "Main Page lists a fee but the dashboard property has no management model", "fmt": "money"})
    elif conn is not None and inc > 0:
        pid = C.PROPERTY_SHEETS[code][0]
        pct = (conn.execute("SELECT management_fee_pct FROM properties WHERE id=?", (pid,)).fetchone() or [None])[0]
        if pct and not sum(r["amount"] for r in rows if r["category"] == "management_fee"):
            checks.append({"metric": "Management model", "workbook": 0.0, "imported": round(inc * pct / 100, 4), "diff": round(inc * pct / 100, 4),
                           "status": "REVIEW", "fmt": "money",
                           "note": f"the dashboard models this property as managed at {pct:g}% and would show a fee of {inc * pct / 100:.2f}, "
                                   "but the workbook records no management fee for it (check the property's model in Settings)"})
    if inc > 0 and days == 0:
        checks.append({"metric": "Income without booked nights", "workbook": None, "imported": None, "diff": None, "status": "REVIEW",
                       "note": f"income {inc:.2f} but Days Booked is 0", "fmt": "money"})
    return checks


def reconcile_business(parsed, ym, want):
    month = parsed["main"]["months"].get(ym)
    if not month:
        return []
    biz = round(sum(r["amount"] for r in want["rows"] if r["category"] != "salary"), 4)
    sal = round(sum(r["amount"] for r in want["rows"] if r["category"] == "salary"), 4)
    echo = sum(v or 0 for v in month["echoes"].values())
    checks = [
        _check("General (sum of itemised rows)", month["general_total"], biz, note="the sheet's own 'General' subtotal; never imported on top of the items"),
        _check("Main Page total", month["total"], round(biz + echo + sal, 4), note="items + property echo rows + salaries"),
    ]
    for code, value in month["echoes"].items():
        sheet_costs = (parsed["properties"].get(code, {}).get("summary", {}).get(ym, {}) or {}).get("total_costs")
        if sheet_costs is not None:
            checks.append(_check(f"{code} echo row vs property sheet costs", value, round(-sheet_costs, 4),
                                 note="echo of the property sheet's costs; NOT a business cost, not imported"))
    return checks


# -------------------------------------------------------------------- the plan
def _issues_for(parsed, scope, ym):
    return [i for i in parsed["issues"] if i["scope"] == scope and i["level"] in ("error", "review") and i["month"] in (None, ym)]


def _counts(diff):
    c = {"NEW": 0, "CHANGED": 0, "REMOVED": 0, "UNCHANGED": 0}
    for d in diff:
        c[d["status"]] += 1
    return c


def _plan_item(conn, parsed, code, ym, with_after=True):
    pid, name = C.PROPERTY_SHEETS[code]
    item = {"code": code, "property_id": pid, "name": name, "sheet": f"{code}{parsed['year'] % 100:02d}", "reasons": [], "rows": [],
            "checks": [], "counts": {}, "current": None, "after": None, "days": None, "bookings": {}}
    exists = conn.execute("SELECT id FROM properties WHERE id=?", (pid,)).fetchone()
    if code not in parsed["properties"]:
        item.update(status="missing_sheet")
        item["reasons"].append(f"The workbook has no {item['sheet']} sheet -- this property is left exactly as it is.")
        return item
    if not exists:
        item.update(status="not_in_dashboard")
        item["reasons"].append(f"{name} is mapped but is not a property in the dashboard. Add it first, then import again.")
        return item
    errors = _issues_for(parsed, code, ym)
    errors = [i for i in errors if i["level"] == "error"]
    if errors:
        item.update(status="error")
        item["reasons"] += [i["message"] for i in errors]
        return item
    want = desired_property(parsed, code, ym)
    # range checks on the operating figures
    summary = parsed["properties"][code]["summary"].get(ym, {})
    occ, days = summary.get("occupancy"), want["days"]
    problems = []
    if occ is not None and (occ < 0 or occ > 1.0001):
        problems.append(f"Occupancy {occ:.0%} is outside 0-100%.")
    if days is not None and days < 0:
        problems.append(f"Days Booked is negative ({days}).")
    if days is not None and days > days_in(ym):
        problems.append(f"Days Booked ({days}) is more than the days in the month ({days_in(ym)}).")
    if days is not None and float(days) != int(days):
        problems.append(f"Days Booked ({days}) is not a whole number.")
    if problems:
        item.update(status="error")
        item["reasons"] += problems
        return item
    cur_tx, cur_agg = current_rows(conn, pid, ym)
    diff = diff_rows(cur_tx, want["rows"])
    item["rows"], item["counts"] = diff, _counts(diff)
    item["checks"] = reconcile_property(parsed, code, ym, want, conn)
    item["days"], item["notes"] = days or 0, want["notes"]
    item["bookings"] = {"current_nights": agg_nights(cur_agg), "workbook_nights": int(days or 0)}
    item["bookings"]["changed"] = item["bookings"]["current_nights"] != item["bookings"]["workbook_nights"]
    changed = item["counts"]["NEW"] + item["counts"]["CHANGED"] + item["counts"]["REMOVED"]
    item["change_count"] = changed + (1 if item["bookings"]["changed"] else 0)
    item["want_summary"] = {"income": round(sum(r["amount"] for r in want["rows"] if r["direction"] == "income"), 2),
                            "costs": round(sum(r["amount"] for r in want["rows"] if r["direction"] == "expense"), 2)}
    if not want["rows"] and not (days or 0) and not cur_tx and not cur_agg:
        item["status"] = "no_activity"
        return item
    item["status"] = "review" if any(c["status"] == "REVIEW" for c in item["checks"]) else "ok"
    if not want["rows"] and (cur_tx or cur_agg):
        item["status"] = "review"
        item["reasons"].append(f"The workbook is blank for this month but the dashboard has {len(cur_tx)} row(s) -- importing would remove them.")
    if item["change_count"] == 0 and item["status"] == "ok":
        item["status"] = "unchanged"
    return item


def plan_month(conn, parsed, ym, with_after=True):
    """The full preview for one month. `with_after` runs each property through the real KPI code on a rolled-back copy."""
    plan = {"ym": ym, "year": parsed["year"], "properties": [], "business": None, "global_errors": [], "unmapped": [], "notes": []}
    if parsed["year"] is None:
        plan["global_errors"] = [i["message"] for i in parsed["issues"] if i["level"] == "error"]
        return plan
    plan["global_errors"] = [i["message"] for i in parsed["issues"] if i["level"] == "error" and i["scope"] is None and i["month"] is None
                             and i["code"] in ("no_year", "main_layout")]
    for name, (role, detail) in parsed["roles"].items():
        if role == "unmapped":
            plan["unmapped"].append({"sheet": name, "message": "New property detected -- map this sheet before importing."})
    from . import apply as A          # local import: apply builds on plan
    for code in C.PROPERTY_SHEETS:
        item = _plan_item(conn, parsed, code, ym)
        if item["status"] in ("ok", "review", "unchanged", "no_activity"):
            item["current"] = dashboard_view(conn, item["property_id"], ym)
            if with_after and item["status"] in ("ok", "review"):
                with simulate(conn):
                    A.write_changes(conn, item, None, ym)
                    item["after"] = dashboard_view(conn, item["property_id"], ym)
            else:
                item["after"] = item["current"]
        plan["properties"].append(item)
    # business costs
    errors = [i for i in _issues_for(parsed, "MAIN", ym) if i["level"] == "error"]
    biz = {"property_id": BUSINESS_ID, "name": "Business costs (Main Page)", "status": "ok", "reasons": [], "rows": [], "checks": [], "counts": {}}
    exists = conn.execute("SELECT id FROM properties WHERE id=?", (BUSINESS_ID,)).fetchone()
    if ym not in parsed["main"]["months"]:
        biz["status"] = "missing_sheet"
        biz["reasons"].append("The Main Page has no column for this month.")
    elif not exists:
        biz["status"] = "not_in_dashboard"
        biz["reasons"].append("The business cost centre does not exist in the dashboard.")
    elif errors:
        biz["status"] = "error"
        biz["reasons"] += [i["message"] for i in errors]
    else:
        want = desired_business(parsed, ym)
        cur_tx, _ = current_rows(conn, BUSINESS_ID, ym)
        diff = diff_rows(cur_tx, want["rows"])
        biz.update(rows=diff, counts=_counts(diff), checks=reconcile_business(parsed, ym, want))
        biz["change_count"] = biz["counts"]["NEW"] + biz["counts"]["CHANGED"] + biz["counts"]["REMOVED"]
        biz["bookings"] = {}
        biz["days"] = None
        biz["status"] = "review" if any(c["status"] == "REVIEW" for c in biz["checks"]) else "ok"
        if biz["change_count"] == 0 and biz["status"] == "ok":
            biz["status"] = "unchanged"
        if any(r["category"] == "salary" and _norm(r["description"]).split(" ")[0] in ("dado", "faris") for r in want["rows"]):
            biz["reasons"].append("Owner pay rows (Dado, Faris salary) are imported as salary, as the history already does for Dado.")
        biz["current"] = dashboard_view(conn, BUSINESS_ID, ym)
        if with_after and biz["status"] in ("ok", "review"):
            with simulate(conn):
                A.write_changes(conn, biz, None, ym)
                biz["after"] = dashboard_view(conn, BUSINESS_ID, ym)
        else:
            biz["after"] = biz["current"]
    plan["business"] = biz
    return plan


def month_overview(conn, parsed):
    """Which workbook months differ from the dashboard -- so a person can pick 'September only'."""
    out = []
    if parsed["year"] is None:
        return out
    for m in range(1, 13):
        ym = make_ym(parsed["year"], m)
        has_data = any(parsed["properties"].get(c, {}).get("summary", {}).get(ym, {}).get("income")
                       or any(parsed["properties"].get(c, {}).get(b, {}).get(ym, {}).get("items") for b in ("opex", "capex", "income"))
                       for c in parsed["properties"]) or bool(parsed["main"]["months"].get(ym, {}).get("items"))
        if not has_data:
            continue
        changed, props = 0, []
        for code in C.PROPERTY_SHEETS:
            if code not in parsed["properties"]:
                continue
            pid = C.PROPERTY_SHEETS[code][0]
            if not conn.execute("SELECT 1 FROM properties WHERE id=?", (pid,)).fetchone():
                continue
            want = desired_property(parsed, code, ym)
            cur_tx, cur_agg = current_rows(conn, pid, ym)
            d = _counts(diff_rows(cur_tx, want["rows"]))
            n = d["NEW"] + d["CHANGED"] + d["REMOVED"] + (1 if agg_nights(cur_agg) != int(want["days"] or 0) else 0)
            if n:
                changed += n
                props.append(C.PROPERTY_SHEETS[code][1])
        biz_changed = 0
        if conn.execute("SELECT 1 FROM properties WHERE id=?", (BUSINESS_ID,)).fetchone() and ym in parsed["main"]["months"]:
            d = _counts(diff_rows(current_rows(conn, BUSINESS_ID, ym)[0], desired_business(parsed, ym)["rows"]))
            biz_changed = d["NEW"] + d["CHANGED"] + d["REMOVED"]
        out.append({"ym": ym, "changed_rows": changed + biz_changed, "properties": props, "business_changed": biz_changed})
    return out
