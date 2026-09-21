"""Rule-based attention/insight findings -- kept deliberately separate from
services/kpis.py, which stays purely mathematical. Each find_* function
looks at one narrow thing and returns a list of {type, text} findings;
compute_insights() is the single entry point routes call, merging and
capping them. Every finding must be specific and numerical -- no vague
AI-style commentary, and nothing here is invented, only read from the
database (see the brief's "no fake UI" / "no generic AI insights" rules)."""
import services.kpis as kpis
from services.common import pct_delta
from services.completeness import completeness_for, DOC_TYPE_LABELS


def _f(kind, text, group, label, endpoint, params=None, anchor=None):
    """A finding with the place to go next: group (Data / Performance / Costs)
    and one action, as an endpoint + params so the template can url_for it."""
    return {"type": kind, "text": text, "group": group,
            "action": {"label": label, "endpoint": endpoint, "params": params or {}, "anchor": anchor}}


def _ym_params(y, m, ey, em, pid):
    return {"from": f"{y}-{m:02d}-01", "to": f"{ey}-{em:02d}-01", "property": pid, "compare": "previous_period"}


def find_revenue_declines(conn, flats, start, end, pstart, pend):
    findings = []
    for p in flats:
        cur_rev = kpis.revenue(conn, p["id"], start, end)
        prev_rev = kpis.revenue(conn, p["id"], pstart, pend)
        d = pct_delta(cur_rev, prev_rev)
        if d is not None and d <= -20 and prev_rev > 50:
            findings.append(_f("warning", f"{p['name']} revenue down {abs(d):.0f}% vs the prior period", "Performance",
                               "Open property", "properties.detail", {"property_id": p["id"]}))
    return findings


def find_cost_anomalies(conn, flats, start, end, pstart, pend, rng):
    findings = []
    for p in flats:
        cur_clean = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE property_id=? AND category='cleaning' AND date>=? AND date<?",
            (p["id"], start, end),
        ).fetchone()[0]
        prev_clean = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE property_id=? AND category='cleaning' AND date>=? AND date<?",
            (p["id"], pstart, pend),
        ).fetchone()[0]
        cd = pct_delta(cur_clean, prev_clean)
        if cd is not None and cd >= 50 and prev_clean > 20:
            findings.append(_f("warning", f"{p['name']} cleaning cost up {cd:.0f}% vs the prior period", "Costs",
                               "View cleaning costs", "expenses.index",
                               {**_ym_params(*rng, p["id"]), "t_category": "cleaning"}, "ledger"))
    return findings


def find_occupancy_highlights(conn, flats, start, end):
    findings = []
    for p in flats:
        occ = kpis.occupancy(conn, p["id"], start, end)
        if occ >= 0.9:
            findings.append(_f("positive", f"{p['name']} occupancy {occ * 100:.0f}%", "Performance",
                               "View bookings", "properties.bookings", {"property_id": p["id"]}))
    return findings


def find_occupancy_declines(conn, flats, end_year, end_month):
    """Current month's occupancy vs. that property's own trailing 3-month
    average (excluding the current month) -- flags a property that's
    cooled off relative to its own recent pace, not against a fixed bar."""
    findings = []
    trailing = [kpis.add_months(end_year, end_month, -n) for n in (1, 2, 3)]
    cur_start, cur_end = kpis.month_bounds(end_year, end_month)
    for p in flats:
        cur_occ = kpis.occupancy(conn, p["id"], cur_start, cur_end)
        trailing_occs = []
        for ty, tm in trailing:
            ts, te = kpis.month_bounds(ty, tm)
            if f"{ty}-{tm:02d}" in kpis.months_with_data(conn, p["id"]):
                trailing_occs.append(kpis.occupancy(conn, p["id"], ts, te))
        if len(trailing_occs) < 2:
            continue
        avg = sum(trailing_occs) / len(trailing_occs)
        pts = (cur_occ - avg) * 100
        if pts <= -10:
            findings.append(_f("warning", f"{p['name']} occupancy is {abs(pts):.0f} pts below its 3-month average", "Performance",
                               "View bookings", "properties.bookings", {"property_id": p["id"]}))
    return findings


def find_missing_sources(conn, flats, rng, start):
    findings = []
    no_docs = [p["name"] for p in flats if not conn.execute(
        "SELECT 1 FROM documents WHERE property_id=? AND uploaded_at >= ? LIMIT 1", (p["id"], start)
    ).fetchone()]
    if no_docs and rng["choice"] in ("this_month", "last_month"):
        findings.append(_f("info", f"{len(no_docs)} propert{'y has' if len(no_docs) == 1 else 'ies have'} not uploaded any documents for {rng['display']}",
                           "Data", "Upload documents", "documents.index"))
    pending = conn.execute("SELECT COUNT(*) FROM documents WHERE status NOT IN ('confirmed')").fetchone()[0]
    if pending:
        findings.append(_f("info", f"{pending} uploaded document{'s' if pending != 1 else ''} awaiting review", "Data",
                           "Review", "documents.index", {"d_status": "review"}))
    return findings


def find_completeness_gaps(conn, flats, start, end, rng):
    """Specific missing-source-type findings from property_data_requirements
    -- more precise than find_missing_sources' "zero documents at all"
    check, e.g. "Riverside is missing its bank statement" rather than just
    flagging a property with nothing uploaded."""
    findings = []
    if rng["choice"] not in ("this_month", "last_month"):
        return findings
    for p in flats:
        c = completeness_for(conn, p["id"], start, end)
        if c and c["missing"] and 0 < c["pct"] < 100:
            labels = ", ".join(DOC_TYPE_LABELS.get(m, m) for m in c["missing"])
            findings.append(_f("info", f"{p['name']} is missing its {labels} for {rng['display']}", "Data",
                               "Upload documents", "documents.index"))
    return findings


def compute_insights(conn, flats, rng):
    start, end = kpis.range_bounds(rng["start_year"], rng["start_month"], rng["end_year"], rng["end_month"])
    pstart, pend = kpis.range_bounds(*kpis.prior_period(rng["start_year"], rng["start_month"], rng["end_year"], rng["end_month"]))
    findings = [
        *find_revenue_declines(conn, flats, start, end, pstart, pend),
        *find_occupancy_declines(conn, flats, rng["end_year"], rng["end_month"]),
        *find_occupancy_highlights(conn, flats, start, end),
        *find_cost_anomalies(conn, flats, start, end, pstart, pend, (rng["start_year"], rng["start_month"], rng["end_year"], rng["end_month"])),
        *find_completeness_gaps(conn, flats, start, end, rng),
        *find_missing_sources(conn, flats, rng, start),
    ]
    order = {"warning": 0, "info": 1, "positive": 2}
    return sorted(findings, key=lambda f: order.get(f["type"], 3))[:20]
