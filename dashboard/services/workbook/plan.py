"""Turn the parsed workbook into a per-month import PLAN, without writing anything.

For each property in the workbook (and the business cost centre) and the selected month:
  * the ledger rows the workbook says should exist ("desired"),
  * a row-level diff against what the dashboard holds from earlier workbook/Excel
    imports (NEW / CHANGED / REMOVED / UNCHANGED),
  * reconciliation checks: workbook control totals vs the detail that would be imported,
  * CURRENT dashboard figures, the WORKBOOK's own figures, and the figures AFTER the import
    (computed by running the real KPI code against a rolled-back copy of the change),
  * for a property that is not in the dashboard yet: a NEW PROPERTY proposal that a person confirms.

Workbook month-summary fields (Net Profit, Operating Profit, Income, Total Costs,
Opex, Capex, Occupancy, Days Booked) are CONTROL TOTALS: they validate the detail
and are never written as ledger rows. Days Booked is the one authoritative
operating figure; it becomes a monthly-aggregate booking, exactly like the history.
"""
import calendar
import contextlib
import re
import statistics

import db
from services import kpis, sources as src
from . import config as C
from . import identity
from .reader import ym as make_ym

BUSINESS_ID = "general-overheads"
AGG = src.AGGREGATE_SOURCES
_AGG_IN = "('" + "','".join(AGG) + "')"
SUSPECT_MIN = 50.0          # a business row this large that equals a property row to the penny is flagged as a possible duplicate


# ------------------------------------------------------------------ dates
def bounds(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return kpis.month_bounds(y, m)


def days_in(ym):
    return calendar.monthrange(int(ym[:4]), int(ym[5:7]))[1]


def prev_month(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _r2(x):
    return round(float(x or 0), 2)


def _norm(text):
    return " ".join((text or "").lower().split())


# ------------------------------------------------------------- identities
def _clean_title(title, code):
    t = (title or "").strip() if isinstance(title, str) else ""
    if not t:
        return code
    return t.title() if t.isupper() else t


def model_evidence(parsed, code, aliases):
    """What the workbook itself says about how a property is run.
    -> {"model": 'managed'|'operated'|None, "pct": float|None, "evidence": [..], "confidence": 'high'|'medium'|'none'}"""
    keys = {identity.norm(a) for a in aliases} | {identity.norm(code)}
    evidence, models, pct = [], set(), None
    for section, labels in (parsed["main"].get("sections") or {}).items():
        for label, has_value in labels.items():
            if identity.norm(label) in keys:
                if section == "r2r":
                    models.add("operated")
                    evidence.append("listed under R2R (rent-to-rent) in the Main Page income block")
                elif section == "management sa":
                    models.add("managed")
                    evidence.append("listed under Management SA in the Main Page income block" + ("" if has_value else " (no fee recorded yet)"))
    prop = parsed["properties"].get(code, {})
    ratios = []
    fee_row = False
    for blk in ("opex", "capex"):
        for ym_, month in prop.get(blk, {}).items():
            for item in month.get("items", []):
                if C.categorise(item["label"]) == "management_fee":
                    fee_row = True
                    m = re.search(r"\((\d+(?:\.\d+)?)\s*%\)", item["label"])
                    if m and pct is None:
                        pct = float(m.group(1))
                    inc = (prop["summary"].get(ym_, {}) or {}).get("income")
                    if inc:
                        ratios.append(item["amount"] / inc * 100)
    if fee_row:
        models.add("managed")
        evidence.append("the property sheet carries a management fee row" + (f" ({pct:g}%)" if pct else ""))
    if pct is None:
        for ym_, month in parsed["main"]["months"].items():
            fee = month.get("fees", {}).get(code)
            inc = (prop.get("summary", {}).get(ym_, {}) or {}).get("income")
            if fee and inc:
                ratios.append(fee / inc * 100)
        if ratios and max(ratios) - min(ratios) < 0.5:
            pct = round(statistics.median(ratios) * 2) / 2
            evidence.append(f"Main Page fee is consistently about {pct:g}% of income")
    if len(models) == 1:
        model = next(iter(models))
        return {"model": model, "pct": pct if model == "managed" else None, "evidence": evidence,
                "confidence": "high" if (model == "operated" or pct is not None) else "medium"}
    if len(models) > 1:
        return {"model": None, "pct": None, "evidence": evidence + ["the workbook points both ways"], "confidence": "none"}
    return {"model": None, "pct": None, "evidence": evidence or ["the workbook says nothing about how it is run"], "confidence": "none"}


def identity_map(conn, parsed):
    """{code: {"pid", "name", "exists", "candidate", "proposal"}} for every property sheet in the workbook plus every mapped code."""
    mapped = identity.mapping(conn)
    out = {}
    for code, m in mapped.items():
        out[code] = {"pid": m["pid"], "name": m["name"], "exists": m["exists"], "candidate": False, "main_only": m.get("main_only", False)}
    for code, prop in parsed["properties"].items():
        if code not in out:
            name = _clean_title(prop.get("title"), code)
            # an exact / normalised alias that already belongs to a property means this sheet is THAT property, not a new one
            found, how = identity.resolve(conn, code)
            if not found:
                found, how = identity.resolve(conn, name)
            if found:
                row = conn.execute("SELECT name FROM properties WHERE id=?", (found,)).fetchone()
                out[code] = {"pid": found, "name": row["name"], "exists": True, "candidate": False, "matched_by": how}
                continue
            out[code] = {"pid": db.unique_slug(conn, db.slugify(name)), "name": name, "exists": False, "candidate": True,
                         "name_source": "the sheet's title cell (not confirmed)"}
    for code, info in out.items():
        if not info["exists"] and (code in parsed["properties"] or info.get("main_only")):
            cfg = C.MAIN_ONLY_PROPERTIES.get(code)
            aliases = [info["name"], code, *C.PROPERTY_ALIASES.get(code, []), *(cfg["aliases"] if cfg else [])]
            ev = model_evidence(parsed, code, aliases)
            decided = C.NEW_PROPERTY_DEFAULTS.get(info["pid"])
            if cfg:
                decided = {"model": cfg["model"], "pct": cfg["pct"], "note": "confirmed by you, 6 Oct 2026: " + cfg["note"]}
            if decided:
                ev = {"model": decided["model"], "pct": decided["pct"], "evidence": ev["evidence"] + [decided["note"]], "confidence": "confirmed"}
            info["proposal"] = {
                "property_id": info["pid"], "name": info["name"], "sheet": ("Main Page only" if code in C.MAIN_ONLY_PROPERTIES else f"{code}{parsed['year'] % 100:02d}"), "code": code,
                "aliases": sorted(set(aliases)),
                "model": ev["model"], "pct": ev["pct"], "evidence": ev["evidence"], "confidence": ev["confidence"],
                "name_source": info.get("name_source") or "confirmed by you, 6 Oct 2026",
                "pct_unknown_ok": bool(cfg and cfg["pct"] is None), "main_only": bool(cfg),
                "confirmed": False}
    return out


def pid_for(conn, parsed, code):
    return identity_map(conn, parsed)[code]["pid"]


# ------------------------------------------------------------ desired rows
def _tx(pid, ym, direction, description, amount, category, capex=0, vendor=None, ref=None):
    return {"property_id": pid, "date": f"{ym}-01", "vendor": vendor or None, "description": description,
            "amount": round(float(amount), 4), "direction": direction, "category": category,
            "capex": int(capex), "source": "workbook", "source_ref": ref}


def desired_property(parsed, code, ym, pid=None, expenses_only=False):
    """The ledger rows and operating days the workbook asks for, for one property-month."""
    pid = pid or C.PROPERTY_SHEETS[code][0]
    prop = parsed["properties"][code]
    rows, notes = [], []
    detail = (parsed.get("breakdown", {}).get(code) or {}).get(ym)
    has_detail = bool(detail and detail["items"])
    for item in ([] if expenses_only else prop["income"].get(ym, {}).get("items", [])):
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
    fee_main = None if expenses_only else parsed["main"]["months"].get(ym, {}).get("fees", {}).get(code)
    if fee_main and not any(r["category"] == "management_fee" for r in rows):
        # the sheet has no fee row but the Main Page records the month's fee: record it, so Management Fee Earned is the
        # workbook's figure rather than the property's percentage applied to income
        fee = _tx(pid, ym, "expense", "Management fee (Main Page)", fee_main, "management_fee", 0, None, f"Main Page{parsed['year'] % 100:02d} fee")
        fee["_main_fee"] = True
        rows.append(fee)
        notes.append("management fee taken from the Main Page (the property sheet has no fee row)")
    summary = prop["summary"].get(ym, {})
    days, derived = (None if expenses_only else summary.get("days")), False
    if days is None and summary.get("occupancy") and not expenses_only:
        days, derived = round(summary["occupancy"] * days_in(ym)), True
        notes.append(f"Days Booked blank; derived {days} from occupancy")
    return {"rows": rows, "days": days, "days_derived": derived, "notes": notes, "has_detail": has_detail}


def desired_business(parsed, ym, excluded=None):
    excluded = excluded or set()
    month = parsed["main"]["months"].get(ym)
    if not month:
        return {"rows": [], "notes": [], "excluded": []}
    rows, skipped = [], []
    for item in month["items"]:
        if item["ref"] in excluded:
            skipped.append(item)
            continue
        rows.append(_tx(BUSINESS_ID, ym, "expense", item["label"], item["amount"], C.categorise(item["label"], business=True), 0, None, item["ref"]))
    for item in month["salaries"]:
        label = item["label"]
        desc = label if "salary" in label.lower() else f"{label} salary"
        rows.append(_tx(BUSINESS_ID, ym, "expense", desc, item["amount"], "salary", 0, None, item["ref"]))
    return {"rows": rows, "notes": [], "excluded": skipped}


def business_suspects(parsed, ym, distinct_refs=None, persisted=None):
    """Main Page business rows that equal a property-level row of the same month to the penny (and are not trivial):
    possible double counts. Flagged, never removed automatically. A row you have said is a genuine separate expense
    (`distinct`, remembered by month + label + amount) is shown but no longer flagged."""
    month = parsed["main"]["months"].get(ym)
    out = []
    if not month:
        return out
    prop_rows = []
    for code, prop in parsed["properties"].items():
        for blk in ("opex", "capex"):
            prop_rows += [(code, i["label"], i["amount"]) for i in prop[blk].get(ym, {}).get("items", [])]
        prop_rows += [(code, i["description"], i["amount"]) for i in (parsed.get("breakdown", {}).get(code) or {}).get(ym, {}).get("items", [])]
    for item in month["items"]:
        if abs(item["amount"]) < SUSPECT_MIN:
            continue
        hits = [(c, label) for c, label, amount in prop_rows if abs(amount - item["amount"]) < 0.005]
        if hits:
            key = (_norm(item["label"]), round(item["amount"], 2))
            is_distinct = (item["ref"] in distinct_refs) if distinct_refs is not None else (key in (persisted or set()))
            out.append({"ref": item["ref"], "label": item["label"], "amount": item["amount"], "matches": hits[:3], "key": key, "distinct": is_distinct})
    return out


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

    def take(predicate_old, status, note=""):
        nonlocal cur, want
        for n in list(want):
            for o in cur:
                if predicate_old(o, n):
                    out.append({"status": status, "old": o, "new": n, "note": note})
                    want.remove(n)
                    cur.remove(o)
                    break

    take(lambda o, n: kk(o) == kk(n) and _r2(o["amount"]) == _r2(n["amount"]), "UNCHANGED")
    take(lambda o, n: kk(o) == kk(n), "CHANGED")
    take(lambda o, n: (o["direction"], int(o["capex"]), _r2(o["amount"])) == (n["direction"], int(n["capex"]), _r2(n["amount"])),
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
    prop = conn.execute("SELECT type, management_fee_pct, is_managed FROM properties WHERE id=?", (pid,)).fetchone()
    expense = lambda cond="": conn.execute(
        f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE property_id=? AND direction='expense' AND date>=? AND date<? {cond}",
        (pid, s, e)).fetchone()[0]
    if prop and prop["type"] == "overhead":
        return {"model": "business", "business_costs": _r2(expense()), "revenue": 0.0, "property_costs": 0.0, "management_fee": 0.0,
                "days": 0, "occupancy": 0.0, "profit": 0.0, "total_expenses": _r2(expense())}
    managed = bool(prop and (prop["management_fee_pct"] or prop["is_managed"]))
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


def workbook_view(parsed, code, ym, want, managed):
    """The workbook's OWN figures for the same rows as dashboard_view (None where the workbook control is blank)."""
    prop = parsed["properties"][code]
    s = prop["summary"].get(ym, {})
    sheet_fee = sum(r["amount"] for r in want["rows"] if r["category"] == "management_fee" and not r.get("_main_fee"))
    fee_main = parsed["main"]["months"].get(ym, {}).get("fees", {}).get(code)
    fee = fee_main if fee_main is not None else (sheet_fee or None)
    costs = None if s.get("total_costs") is None else round(-s["total_costs"] - sheet_fee, 2)
    return {"revenue": s.get("income"), "property_costs": costs, "management_fee": fee if managed else None,
            "days": s.get("days"), "occupancy": s.get("occupancy"),
            "profit": (fee if managed else s.get("net"))}


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
def _check(metric, wb, imp, tol=C.TOLERANCE, note="", fmt="money"):
    if wb is None:
        return {"metric": metric, "workbook": None, "imported": imp, "diff": None, "status": "NO CONTROL", "fmt": fmt,
                "note": note or "the workbook's own total for this is blank, so nothing can confirm it",
                "gating": imp not in (0, 0.0, None)}
    diff = round(imp - wb, 4) if imp is not None else None
    ok = diff is not None and abs(diff) <= tol
    return {"metric": metric, "workbook": wb, "imported": imp, "diff": diff, "status": "PASS" if ok else "REVIEW", "note": note, "fmt": fmt, "gating": True}


def _gates(check):
    return check["status"] == "REVIEW" or (check["status"] == "NO CONTROL" and check.get("gating", True))


def reconcile_property(parsed, code, ym, want, conn=None, pid=None, pct=None, managed=None, pre_opening=False):
    managed = bool(pct) if managed is None else managed
    prop = parsed["properties"][code]
    summary = prop["summary"].get(ym, {})
    rows = want["rows"]
    own = [r for r in rows if not r.get("_main_fee")]          # what the property sheet itself counts
    inc = sum(r["amount"] for r in own if r["direction"] == "income")
    opex = sum(r["amount"] for r in own if r["direction"] == "expense" and not r["capex"])
    capex = sum(r["amount"] for r in own if r["direction"] == "expense" and r["capex"])
    days = want["days"] or 0
    ctl_opex, ctl_capex = summary.get("opex"), summary.get("capex")
    ctl_total = None if summary.get("total_costs") is None else -summary["total_costs"]
    if pre_opening:             # before the property opened its summary row is blank by design: the blocks' own totals are the controls
        ob, cb = prop["opex"].get(ym, {}).get("total"), prop["capex"].get(ym, {}).get("total")
        ctl_opex = ob if ctl_opex is None else ctl_opex
        ctl_capex = (cb if cb is not None else 0.0) if ctl_capex is None and ob is not None else ctl_capex
        if ctl_total is None and ob is not None:
            ctl_total = (ctl_opex or 0) + (ctl_capex or 0)
    checks = [
        _check("Income", summary.get("income"), round(inc, 4)),
        _check("Opex", ctl_opex, round(opex, 4), note="the block's own total (pre-opening: the summary row is blank by design)" if pre_opening and summary.get("opex") is None else ""),
        _check("Capex", ctl_capex, round(capex, 4)),
        _check("Total costs", ctl_total, round(opex + capex, 4)),
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
                   "status": "REVIEW" if worst else "PASS", "note": "; ".join(details), "fmt": "money", "gating": True})
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
                       "note": f"{len(detail['items'])} breakdown rows replace the lump", "fmt": "money", "gating": True})
        sub_gap = max(abs((detail["opex"] or 0) - d_o), abs((detail["capex"] or 0) - d_c))
        if sub_gap > C.TOLERANCE:
            checks.append({"metric": "Breakdown subtotals vs rows", "workbook": None, "imported": None, "diff": round(sub_gap, 4),
                           "status": "REVIEW", "note": "OPEX/CAPEX subtotal differs from the rows its own formula counts", "fmt": "money", "gating": True})
    # management fee: Main Page figure vs the fee that will be on the books
    fee_wb = parsed["main"]["months"].get(ym, {}).get("fees", {}).get(code)
    sheet_fee = sum(r["amount"] for r in own if r["category"] == "management_fee")
    if fee_wb is not None and sheet_fee:
        checks.append(_check("Management fee", fee_wb, round(sheet_fee, 4), note="Main Page vs the fee row on the property sheet"))
    fee_used = sheet_fee or (fee_wb or 0)
    if pct and inc and fee_used:
        rate = fee_used / inc * 100
        checks.append({"metric": "Management fee rate", "workbook": round(rate / 100, 4), "imported": round(pct / 100, 4), "diff": round((rate - pct) / 100, 4),
                       "status": "PASS" if abs(rate - pct) <= 0.05 else "REVIEW", "fmt": "pct", "gating": True,
                       "note": f"the fee is {rate:.2f}% of the imported income; the property's setting is {pct:g}%"})
    elif fee_wb and not sheet_fee and not managed:
        checks.append({"metric": "Management fee", "workbook": fee_wb, "imported": 0.0, "diff": -fee_wb, "status": "REVIEW", "fmt": "money", "gating": True,
                       "note": "Main Page lists a fee but the property has no management model"})
    elif managed and pct and inc > 0 and not fee_used:
        checks.append({"metric": "Management model", "workbook": 0.0, "imported": round(inc * pct / 100, 4), "diff": round(inc * pct / 100, 4),
                       "status": "REVIEW", "fmt": "money", "gating": True,
                       "note": f"the dashboard models this property as managed at {pct:g}% and would show a fee of {inc * pct / 100:.2f}, "
                               "but the workbook records no management fee for it"})
    # labelled rows the workbook's own total does not count are not imported: say so, with the money
    skipped = [(b, e) for b in ("opex", "capex", "income") for e in prop[b].get(ym, {}).get("excluded", [])]
    if skipped:
        checks.append({"metric": "Rows the workbook's own total does not count", "workbook": None, "imported": 0.0, "diff": round(sum(e["amount"] for _b, e in skipped), 4),
                       "status": "REVIEW", "fmt": "money", "gating": True,
                       "note": "not imported (outside the block's own SUM): " + "; ".join(f"{e['label']} {e['amount']:.2f} ({e['ref']})" for _b, e in skipped)})
    # sign-flipped copy of last month's rows (a reversal / credit, not new spending)
    prev = prev_month(ym)
    prev_items = [(i["label"].strip().lower(), round(i["amount"], 2)) for blk in ("opex", "capex") for i in prop[blk].get(prev, {}).get("items", [])]
    now_items = [i for blk in ("opex", "capex") for i in prop[blk].get(ym, {}).get("items", []) if i["amount"] < 0]
    flipped = [i for i in now_items if (i["label"].strip().lower(), round(-i["amount"], 2)) in prev_items]
    if len(flipped) >= 3:
        checks.append({"metric": "Reversal of last month's costs", "workbook": None, "imported": round(sum(i["amount"] for i in flipped), 4), "diff": None,
                       "status": "REVIEW", "fmt": "money", "gating": True,
                       "note": f"{len(flipped)} cost rows are the exact negatives of {prev}'s rows (the sheet's formulas multiply them by -1): "
                               "a reversal or credit of earlier spending, so property costs for the month come out negative"})
    if inc > 0 and days == 0:
        checks.append({"metric": "Income without booked nights", "workbook": None, "imported": None, "diff": None, "status": "REVIEW",
                       "note": f"income {inc:.2f} but Days Booked is 0", "fmt": "money", "gating": True})
    return checks


def reconcile_business(parsed, ym, want, suspects, excluded):
    month = parsed["main"]["months"].get(ym)
    if not month:
        return []
    skipped = sum(i["amount"] for i in want.get("excluded", []))
    biz = round(sum(r["amount"] for r in want["rows"] if r["category"] != "salary") + skipped, 4)
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
    for s in suspects:
        where = ", ".join(f"{c}: {l}" for c, l in s["matches"])
        if s["distinct"] and s["ref"] not in excluded:
            checks.append({"metric": f"Separate expense: {s['label']}", "workbook": s["amount"], "imported": s["amount"], "diff": None, "status": "PASS", "fmt": "money",
                           "note": f"you have confirmed this is a genuine business cost, not a copy of a property cost (same amount as {where})", "gating": False})
        elif s["ref"] in excluded:
            checks.append({"metric": f"Possible duplicate: {s['label']}", "workbook": s["amount"], "imported": 0.0, "diff": None, "status": "PASS", "fmt": "money",
                           "note": f"excluded from the import by you (equals a property row: {where})", "gating": False})
        else:
            checks.append({"metric": f"Possible duplicate: {s['label']}", "workbook": s["amount"], "imported": s["amount"], "diff": None, "status": "REVIEW", "fmt": "money",
                           "note": f"the same amount to the penny is already a property cost this month ({where}). Exclude it below if it is the same expense.",
                           "gating": True})
    return checks


# -------------------------------------------------------------------- the plan
def _issues_for(parsed, scope, ym):
    return [i for i in parsed["issues"] if i["scope"] == scope and i["level"] in ("error", "review") and i["month"] in (None, ym)]


def _counts(diff):
    c = {"NEW": 0, "CHANGED": 0, "REMOVED": 0, "UNCHANGED": 0}
    for d in diff:
        c[d["status"]] += 1
    return c


def _starts(conn, info):
    if not info["exists"]:
        return None
    return (conn.execute("SELECT start_date FROM properties WHERE id=?", (info["pid"],)).fetchone() or [None])[0]


def _apply_confirmation(info, confirm, item):
    """The NEW PROPERTY proposal, overlaid with what the person submitted (strictly: a cleared box is not a default)."""
    prop = dict(info["proposal"])
    c = (confirm or {}).get(info["pid"])
    if c:
        prop["name"] = (c.get("name") or prop["name"]).strip()
        prop["model"] = c.get("model") or None
        prop["pct"] = c.get("pct")
        prop["confirmed"] = bool(prop["name"] and prop["model"] in ("managed", "operated")
                                 and (prop["model"] == "operated" or prop["pct"] or prop.get("pct_unknown_ok")))
        item["name"] = prop["name"]
    item["new_property"] = prop
    return prop


def _new_property_reasons(item):
    prop = item["new_property"]
    item["reasons"].append("NEW PROPERTY DETECTED. Nothing is created until you confirm and apply.")
    if not prop["model"]:
        item["reasons"].append("The workbook does not say whether this is managed or operated: choose below.")
    elif prop["model"] == "managed" and not prop["pct"]:
        if prop.get("pct_unknown_ok"):
            item["reasons"].append("The management fee percentage is not in the workbook, so none is invented: it is created as managed with NO percentage "
                                   "(only recorded fees count; nothing is estimated). Configuration still needed: its fee %.")
        else:
            item["reasons"].append("It looks managed, but no fee percentage can be read from the workbook: enter it below.")


def _plan_main_only(conn, parsed, code, ym, info, confirm):
    """A managed property that exists only on the Main Page: import what the Main Page actually records (its management fee) and nothing else."""
    pid, name = info["pid"], info["name"]
    item = {"code": code, "property_id": pid, "name": name, "sheet": "Main Page only", "main_only": True, "reasons": [], "rows": [], "checks": [], "counts": {},
            "current": None, "after": None, "days": None, "bookings": {}, "new_property": None}
    fee = parsed["main"]["months"].get(ym, {}).get("fees", {}).get(code)
    ref = parsed["main"]["months"].get(ym, {}).get("fee_refs", {}).get(code)
    rows = []
    if fee:
        rows.append(_tx(pid, ym, "expense", "Management fee (Main Page)", fee, "management_fee", 0, None, ref))
    exists = info["exists"]
    cur_tx = current_rows(conn, pid, ym)[0] if exists else []
    if not exists:
        prop = _apply_confirmation(info, confirm, item)
    diff = diff_rows(cur_tx, rows)
    item["rows"], item["counts"] = diff, _counts(diff)
    item["change_count"] = item["counts"]["NEW"] + item["counts"]["CHANGED"] + item["counts"]["REMOVED"]
    pct_known = (conn.execute("SELECT management_fee_pct FROM properties WHERE id=?", (pid,)).fetchone() or [None])[0] if exists else item["new_property"]["pct"]
    item["checks"] = [
        {"metric": "Management fee", "workbook": fee, "imported": round(sum(r["amount"] for r in rows), 4), "diff": 0.0 if fee else None,
         "status": "PASS" if fee else "NO CONTROL", "fmt": "money", "gating": False,
         "note": "the explicit amount the Main Page records for this month (used as recorded, not estimated from a percentage)" if fee else "the Main Page records no fee for this month"},
    ]
    if not pct_known:
        item["checks"].append({"metric": "Fee percentage", "workbook": None, "imported": None, "diff": None, "status": "NO CONTROL", "fmt": "pct", "gating": False,
                               "note": "not established by the workbook, so none is assumed. Configuration still needed (Settings, or when the property is created)."})
    item["reasons"].append("MAIN PAGE ONLY: this property has no sheet of its own. Only its recorded management fee is imported; no income, costs, bookings, "
                           "days or occupancy are invented.")
    item["workbook"] = {"revenue": None, "property_costs": None, "management_fee": fee, "days": None, "occupancy": None, "profit": fee}
    if not rows and not cur_tx:
        item["status"] = "no_activity"
        item["new_property"] = None                        # nothing in this month: nothing to propose
        return item
    item["flagged"] = False
    item["status"] = "ok"
    if not exists:
        item["status"] = "new_property"
        _new_property_reasons(item)
    elif item["change_count"] == 0:
        item["status"] = "unchanged"
    return item


def _plan_item(conn, parsed, code, ym, ident, confirm=None):
    info = ident[code]
    pid, name = info["pid"], info["name"]
    if info.get("main_only") and code not in parsed["properties"]:
        return _plan_main_only(conn, parsed, code, ym, info, confirm)
    item = {"code": code, "property_id": pid, "name": name, "sheet": f"{code}{parsed['year'] % 100:02d}", "reasons": [], "rows": [],
            "checks": [], "counts": {}, "current": None, "after": None, "days": None, "bookings": {}, "new_property": None}
    if code not in parsed["properties"]:
        item.update(status="missing_sheet")
        item["reasons"].append(f"The workbook has no {item['sheet']} sheet -- this property is left exactly as it is.")
        return item
    starts = _starts(conn, info)
    pre_opening = bool(starts and ym < starts[:7])
    if pre_opening:
        prop_data = parsed["properties"][code]
        costs = sum(len(prop_data[b].get(ym, {}).get("items", [])) for b in ("opex", "capex"))
        income_rows = len(prop_data["income"].get(ym, {}).get("items", []))
        if not costs:
            item.update(status="not_active", starts=starts)
            item["reasons"].append(f"NOT ACTIVE: {name} joined the portfolio on {starts}. This month is out of scope, so nothing is expected, imported, flagged or removed"
                                   + (f" (the workbook has {income_rows} income row(s) for it, which are not imported)." if income_rows else "."))
            return item
        item["pre_opening"] = True
        item["starts"] = starts
    errors = [i for i in _issues_for(parsed, code, ym) if i["level"] == "error"]
    if errors:
        item.update(status="error")
        item["reasons"] += [i["message"] for i in errors]
        return item
    want = desired_property(parsed, code, ym, pid, expenses_only=pre_opening)
    summary = parsed["properties"][code]["summary"].get(ym, {})
    occ, days = summary.get("occupancy"), want["days"]
    problems = []
    if not pre_opening:
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
    exists = info["exists"]
    pct, managed = None, False
    if exists:
        pr = conn.execute("SELECT management_fee_pct, is_managed FROM properties WHERE id=?", (pid,)).fetchone()
        pct, managed = (pr["management_fee_pct"], bool(pr["management_fee_pct"] or pr["is_managed"])) if pr else (None, False)
        cur_tx, cur_agg = current_rows(conn, pid, ym)
    else:
        prop = _apply_confirmation(info, confirm, item)
        pct = prop["pct"] if prop["model"] == "managed" else None
        managed = prop["model"] == "managed"
        cur_tx, cur_agg = [], []
    diff = diff_rows(cur_tx, want["rows"])
    item["rows"], item["counts"] = diff, _counts(diff)
    checks = reconcile_property(parsed, code, ym, want, conn, pid, pct, managed, pre_opening=pre_opening)
    if pre_opening:
        keep = ("Opex", "Capex", "Total costs", "Detail rows vs block totals", "Rows the workbook's own total does not count", "Purchases detail vs sheet lump",
                "Breakdown subtotals vs rows")
        checks = [ch for ch in checks if ch["metric"] in keep]
    item["checks"] = checks
    item["days"], item["notes"] = (0 if pre_opening else (days or 0)), want["notes"]
    item["bookings"] = {"current_nights": agg_nights(cur_agg), "workbook_nights": int(days or 0)}
    item["bookings"]["changed"] = False if pre_opening else item["bookings"]["current_nights"] != item["bookings"]["workbook_nights"]
    changed = item["counts"]["NEW"] + item["counts"]["CHANGED"] + item["counts"]["REMOVED"]
    item["change_count"] = changed + (1 if item["bookings"]["changed"] else 0)
    item["workbook"] = workbook_view(parsed, code, ym, want, managed)
    if pre_opening:
        n = len(parsed["properties"][code]["income"].get(ym, {}).get("items", []))
        item["reasons"].append(f"PRE-OPENING COSTS: {name} joined the portfolio on {starts}. Only costs are imported for this month; the property stays NOT ACTIVE "
                               "for revenue, occupancy and data health."
                               + (f" {n} income row(s) in the workbook before the start date are not imported." if n else ""))
    if not want["rows"] and not (days or 0) and not cur_tx and not cur_agg:
        item["status"] = "no_activity"
        item["new_property"] = None
        return item
    item["flagged"] = any(_gates(ch) for ch in item["checks"])
    item["status"] = "review" if item["flagged"] else "ok"
    if not exists:
        item["status"] = "new_property"
        _new_property_reasons(item)
        if item["flagged"]:
            item["reasons"].append("Some checks are flagged (see the details).")
    elif not want["rows"] and (cur_tx or cur_agg):
        item["status"] = "review"
        item["reasons"].append(f"The workbook is blank for this month but the dashboard has {len(cur_tx)} row(s) -- importing would remove them.")
    if exists and item["change_count"] == 0 and item["status"] == "ok":
        item["status"] = "unchanged"
    return item


def plan_month(conn, parsed, ym, with_after=True, excluded=None, confirm=None, distinct=None):
    """The full preview for one month. `with_after` runs each property through the real KPI code on a rolled-back copy.
    `excluded`: Main Page business-row cells the person chose to leave out. `confirm`: {property_id: {name, model, pct}} for new properties."""
    if excluded is None:           # the person's earlier choices for this month
        excluded = [r[0] for r in conn.execute("SELECT source_ref FROM import_exclusions WHERE period=?", (ym,))]
    excluded = set(excluded)
    persisted = {(r[0], round(r[1], 2)) for r in conn.execute("SELECT label_norm, amount FROM import_distinct WHERE period=?", (ym,))}
    plan = {"ym": ym, "year": parsed["year"], "properties": [], "business": None, "global_errors": [], "notes": [], "excluded": sorted(excluded),
            "distinct_refs": sorted(distinct) if distinct is not None else None, "distinct_keys": []}
    if parsed["year"] is None:
        plan["global_errors"] = [i["message"] for i in parsed["issues"] if i["level"] == "error"]
        return plan
    plan["global_errors"] = [i["message"] for i in parsed["issues"] if i["level"] == "error" and i["scope"] is None and i["month"] is None
                             and i["code"] in ("no_year", "main_layout")]
    from . import apply as A          # local import: apply builds on plan
    ident = identity_map(conn, parsed)
    for code in ident:
        item = _plan_item(conn, parsed, code, ym, ident, confirm)
        if item["status"] in ("ok", "review", "unchanged", "no_activity", "new_property"):
            exists = ident[code]["exists"]
            item["current"] = dashboard_view(conn, item["property_id"], ym) if exists else None
            new = item["new_property"]
            simulate_it = with_after and (item["status"] in ("ok", "review") or (
                item["status"] == "new_property" and new["model"] in ("managed", "operated") and (new["model"] == "operated" or new["pct"] or new.get("pct_unknown_ok"))))
            if simulate_it:
                with simulate(conn):
                    A.write_changes(conn, item, None, ym)
                    item["after"] = dashboard_view(conn, item["property_id"], ym)
            else:
                item["after"] = item["current"]
        plan["properties"].append(item)
    # business costs
    errors = [i for i in _issues_for(parsed, "MAIN", ym) if i["level"] == "error"]
    biz = {"property_id": BUSINESS_ID, "name": "Business costs (Main Page)", "status": "ok", "reasons": [], "rows": [], "checks": [], "counts": {},
           "suspects": []}
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
        want = desired_business(parsed, ym, excluded)
        cur_tx, _ = current_rows(conn, BUSINESS_ID, ym)
        diff = diff_rows(cur_tx, want["rows"])
        suspects = business_suspects(parsed, ym, distinct, persisted)
        plan["distinct_keys"] = [(ym, s["key"][0], s["key"][1], s["label"]) for s in suspects if s["distinct"] and s["ref"] not in excluded]
        biz.update(rows=diff, counts=_counts(diff), checks=reconcile_business(parsed, ym, want, suspects, excluded), suspects=suspects)
        biz["change_count"] = biz["counts"]["NEW"] + biz["counts"]["CHANGED"] + biz["counts"]["REMOVED"]
        biz["bookings"] = {}
        biz["days"] = None
        biz["flagged"] = any(_gates(c) for c in biz["checks"])
        biz["status"] = "review" if biz["flagged"] else "ok"
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
    ident = identity_map(conn, parsed)
    for m in range(1, 13):
        ym = make_ym(parsed["year"], m)
        has_data = any(parsed["properties"].get(c, {}).get("summary", {}).get(ym, {}).get("income")
                       or any(parsed["properties"].get(c, {}).get(b, {}).get(ym, {}).get("items") for b in ("opex", "capex", "income"))
                       for c in parsed["properties"]) or bool(parsed["main"]["months"].get(ym, {}).get("items"))
        if not has_data:
            continue
        changed, props, new = 0, [], []
        for code, info in ident.items():
            if info.get("main_only") and code not in parsed["properties"]:
                fee = parsed["main"]["months"].get(ym, {}).get("fees", {}).get(code)
                rows = [_tx(info["pid"], ym, "expense", "Management fee (Main Page)", fee, "management_fee", 0)] if fee else []
                d = _counts(diff_rows(current_rows(conn, info["pid"], ym)[0] if info["exists"] else [], rows))
                n = d["NEW"] + d["CHANGED"] + d["REMOVED"]
                if n:
                    changed += n
                    props.append(info["name"])
                    if not info["exists"]:
                        new.append(info["name"])
                continue
            if code not in parsed["properties"]:
                continue
            starts = (conn.execute("SELECT start_date FROM properties WHERE id=?", (info["pid"],)).fetchone() or [None])[0] if info["exists"] else None
            pre = bool(starts and ym < starts[:7])
            if pre and not any(parsed["properties"][code][b].get(ym, {}).get("items") for b in ("opex", "capex")):
                continue                                    # not active yet and no pre-opening costs: nothing to import
            want = desired_property(parsed, code, ym, info["pid"], expenses_only=pre)
            cur_tx, cur_agg = ([], []) if not info["exists"] else current_rows(conn, info["pid"], ym)
            d = _counts(diff_rows(cur_tx, want["rows"]))
            n = d["NEW"] + d["CHANGED"] + d["REMOVED"] + (1 if (not pre and agg_nights(cur_agg) != int(want["days"] or 0)) else 0)
            if n:
                changed += n
                props.append(info["name"])
                if not info["exists"]:
                    new.append(info["name"])
        biz_changed = 0
        if conn.execute("SELECT 1 FROM properties WHERE id=?", (BUSINESS_ID,)).fetchone() and ym in parsed["main"]["months"]:
            d = _counts(diff_rows(current_rows(conn, BUSINESS_ID, ym)[0], desired_business(parsed, ym)["rows"]))
            biz_changed = d["NEW"] + d["CHANGED"] + d["REMOVED"]
        earning = sum(1 for c in parsed["properties"].values() if (c["summary"].get(ym, {}).get("income") or 0) > 0
                      or sum(i["amount"] for i in c["income"].get(ym, {}).get("items", [])) > 0)
        out.append({"ym": ym, "changed_rows": changed + biz_changed, "properties": props, "business_changed": biz_changed,
                    "properties_with_income": earning, "of": len(parsed["properties"]), "new_properties": new})
    return out
