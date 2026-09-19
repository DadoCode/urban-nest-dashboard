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


def find_revenue_declines(conn, flats, start, end, pstart, pend):
    findings = []
    for p in flats:
        cur_rev = kpis.revenue(conn, p["id"], start, end)
        prev_rev = kpis.revenue(conn, p["id"], pstart, pend)
        d = pct_delta(cur_rev, prev_rev)
        if d is not None and d <= -20 and prev_rev > 50:
            findings.append({"type": "warning", "text": f"{p['name']} revenue down {abs(d):.0f}% vs the prior period"})
    return findings


def find_cost_anomalies(conn, flats, start, end, pstart, pend):
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
            findings.append({"type": "warning", "text": f"{p['name']} cleaning cost up {cd:.0f}% vs the prior period"})
    return findings


def find_occupancy_highlights(conn, flats, start, end):
    findings = []
    for p in flats:
        occ = kpis.occupancy(conn, p["id"], start, end)
        if occ >= 0.9:
            findings.append({"type": "positive", "text": f"{p['name']} occupancy {occ * 100:.0f}%"})
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
            findings.append({"type": "warning", "text": f"{p['name']} occupancy is {abs(pts):.0f} pts below its 3-month average"})
    return findings


def find_target_variances(conn, flats, end_year, end_month, is_partial):
    findings = []
    for p in flats:
        rev_target = kpis.dynamic_target(conn, p["id"], end_year, end_month, end_year, end_month, "revenue")
        if not rev_target:
            continue
        start, end = kpis.month_bounds(end_year, end_month)
        actual = kpis.revenue(conn, p["id"], start, end)
        pct = round(actual / rev_target * 100)
        # The target is now a trailing 3-month average, not a fixed goal --
        # "hit 100%" is just "matched recent pace" and would fire constantly
        # as routine noise, so the bar for calling it out is meaningfully
        # ahead of or behind that recent pace, not merely at or under it.
        if pct >= 120:
            findings.append({"type": "positive", "text": f"{p['name']} is at {pct}% of its trailing-average pace"})
        elif not is_partial and pct <= 60:
            findings.append({"type": "warning", "text": f"{p['name']} is at only {pct}% of its trailing-average pace"})
    return findings


def find_missing_sources(conn, flats, rng, start):
    findings = []
    no_docs = [p["name"] for p in flats if not conn.execute(
        "SELECT 1 FROM documents WHERE property_id=? AND uploaded_at >= ? LIMIT 1", (p["id"], start)
    ).fetchone()]
    if no_docs and rng["choice"] in ("this_month", "last_month"):
        findings.append({"type": "info", "text": f"{len(no_docs)} propert{'y has' if len(no_docs) == 1 else 'ies have'} not uploaded any documents for {rng['display']}"})
    pending = conn.execute("SELECT COUNT(*) FROM documents WHERE status NOT IN ('confirmed')").fetchone()[0]
    if pending:
        findings.append({"type": "info", "text": f"{pending} uploaded document(s) awaiting review"})
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
            findings.append({"type": "info", "text": f"{p['name']} is missing its {labels} for {rng['display']}"})
    return findings


def compute_insights(conn, flats, rng):
    start, end = kpis.range_bounds(rng["start_year"], rng["start_month"], rng["end_year"], rng["end_month"])
    pstart, pend = kpis.range_bounds(*kpis.prior_period(rng["start_year"], rng["start_month"], rng["end_year"], rng["end_month"]))
    findings = [
        *find_target_variances(conn, flats, rng["end_year"], rng["end_month"], rng["partial"]),
        *find_revenue_declines(conn, flats, start, end, pstart, pend),
        *find_occupancy_declines(conn, flats, rng["end_year"], rng["end_month"]),
        *find_occupancy_highlights(conn, flats, start, end),
        *find_cost_anomalies(conn, flats, start, end, pstart, pend),
        *find_completeness_gaps(conn, flats, start, end, rng),
        *find_missing_sources(conn, flats, rng, start),
    ]
    return findings[:8]
