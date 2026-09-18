"""Rule-based attention/insight findings -- kept deliberately separate from
services/kpis.py, which stays purely mathematical. Each find_* function
looks at one narrow thing and returns a list of {type, text} findings;
compute_insights() is the single entry point routes call, merging and
capping them. Every finding must be specific and numerical -- no vague
AI-style commentary, and nothing here is invented, only read from the
database (see the brief's "no fake UI" / "no generic AI insights" rules).

find_target_variances() and true occupancy-*decline* detection (vs. the
current occupancy-*highlight* check below) are Phase 4 additions, once the
Overview rebuild has a dedicated Attention card to put them in -- this
module's job for now is establishing where that logic will live."""
import services.kpis as kpis
from services.common import pct_delta


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


def compute_insights(conn, flats, rng):
    start, end = kpis.range_bounds(rng["start_year"], rng["start_month"], rng["end_year"], rng["end_month"])
    pstart, pend = kpis.range_bounds(*kpis.prior_period(rng["start_year"], rng["start_month"], rng["end_year"], rng["end_month"]))
    findings = [
        *find_revenue_declines(conn, flats, start, end, pstart, pend),
        *find_occupancy_highlights(conn, flats, start, end),
        *find_cost_anomalies(conn, flats, start, end, pstart, pend),
        *find_missing_sources(conn, flats, rng, start),
    ]
    return findings[:8]
