"""
The one place every page gets its numbers from. Nothing is pre-computed and
cached in a totals table -- every KPI here is derived, on read, from
`bookings` (reservation-level accommodation revenue) and `transactions`
(everything else, income or expense) for whatever property and date range
is asked for. A month with no rows simply never appears in a series --
callers never need to special-case "is this really zero or just missing."

property_id=None means "the whole portfolio" everywhere below.
"""
import datetime

from services import sources as src


def month_bounds(year, month):
    """[start, end) as ISO date strings for one calendar month."""
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month + 1:02d}-01" if month < 12 else f"{year + 1}-01-01"
    return start, end


def range_bounds(start_year, start_month, end_year, end_month):
    """[start, end) spanning start_year/start_month through end_year/end_month inclusive."""
    start = f"{start_year}-{start_month:02d}-01"
    end = f"{end_year}-{end_month + 1:02d}-01" if end_month < 12 else f"{end_year + 1}-01-01"
    return start, end


def prior_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def add_months(year, month, delta):
    """(year, month) shifted by delta months (may be negative)."""
    total = (year * 12 + (month - 1)) + delta
    return total // 12, total % 12 + 1


def months_in_range(start_year, start_month, end_year, end_month):
    """Inclusive month count, e.g. Jan-Mar = 3."""
    return (end_year - start_year) * 12 + (end_month - start_month) + 1


def shift_range(start_year, start_month, end_year, end_month, delta_months):
    sy, sm = add_months(start_year, start_month, delta_months)
    ey, em = add_months(end_year, end_month, delta_months)
    return sy, sm, ey, em


def prior_period(start_year, start_month, end_year, end_month):
    """The immediately-preceding span of the same length."""
    n = months_in_range(start_year, start_month, end_year, end_month)
    return shift_range(start_year, start_month, end_year, end_month, -n)


def same_period_last_year(start_year, start_month, end_year, end_month):
    return shift_range(start_year, start_month, end_year, end_month, -12)


def _prop_clause(property_id, column="property_id"):
    return (f"AND {column} = ?", (property_id,)) if property_id else ("", ())



# Booking-derived numbers (revenue, nights, reservations) come from exactly ONE
# source per property and month -- the Excel history or the detailed
# reservations -- chosen explicitly (services/sources.py). Adding both would
# double count and letting one reservation replace a month would destroy it.
# A stay that spans two months contributes each month's share under THAT
# month's source. Costs and everything else are untouched.

def _income(conn, property_id, start, end, accommodation_only=False):
    """Income transactions in [start, end). The Excel history's lumps count
    only in months whose active source is the Excel history."""
    clause, params = _prop_clause(property_id)
    category = "AND category='booking_income'" if accommodation_only else ""
    S = src.Sources(conn, property_id, start, end)
    total = 0.0
    for r in conn.execute(
            f"""SELECT property_id, substr(date,1,7) ym, source, COALESCE(SUM(amount),0) amt FROM transactions
                WHERE direction='income' {category} AND date>=? AND date<? {clause} GROUP BY property_id, ym, source""",
            (start, end, *params)):
        if r["source"] in src.AGGREGATE_SOURCES and S.active(r["property_id"], r["ym"]) == src.DETAILED:
            continue
        total += r["amt"]
    return total


def _booking_pieces(conn, property_id, start, end):
    """[(row, nights, net_share)] for every stay overlapping [start, end),
    counting only the nights that fall in a month whose active source owns
    that kind of row. Detailed reservations are de-duplicated first."""
    clause, params = _prop_clause(property_id)
    rows = conn.execute(
        f"""SELECT id, property_id, reservation_id, check_in, check_out, net_revenue, source FROM bookings
            WHERE status='confirmed' AND check_in<? AND check_out>? {clause}""", (end, start, *params)).fetchall()
    S = src.Sources(conn, property_id, start, end)
    legacy = [r for r in rows if r["source"] in src.AGGREGATE_SOURCES]
    detailed = src.dedupe_detailed([r for r in rows if r["source"] not in src.AGGREGATE_SOURCES])
    out = []
    for kind, group in ((src.LEGACY, legacy), (src.DETAILED, detailed)):
        for r in group:
            nights = share = 0.0
            for ym, lo, hi in src.stay_pieces(r, start, end):
                if S.active(r["property_id"], ym) == kind:
                    nights += src.nights_inside(r, lo, hi)
                    share += src.prorate(r, lo, hi, r["net_revenue"] or 0.0)
            if nights:
                out.append((r, int(nights), share))
    return out


def revenue(conn, property_id, start, end):
    """All accommodation + ancillary income for the period."""
    return _income(conn, property_id, start, end) + sum(share for _r, _n, share in _booking_pieces(conn, property_id, start, end))


def accommodation_revenue(conn, property_id, start, end):
    """Revenue narrowed to actual stays (booking_income transactions +
    reservations) -- excludes ancillary/other income -- for ADR/RevPAR."""
    return _income(conn, property_id, start, end, accommodation_only=True) + sum(share for _r, _n, share in _booking_pieces(conn, property_id, start, end))


def costs(conn, property_id, start, end, capex=None):
    clause, params = _prop_clause(property_id)
    capex_clause = "" if capex is None else f"AND capex = {1 if capex else 0}"
    return conn.execute(
        f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE direction='expense' {capex_clause} AND date>=? AND date<? {clause}",
        (start, end, *params),
    ).fetchone()[0]


def net_profit(conn, property_id, start, end):
    return revenue(conn, property_id, start, end) - costs(conn, property_id, start, end)


def booked_nights(conn, property_id, start, end):
    """Nights inside [start, end) from the source that owns each month -- a
    stay spanning the boundary contributes only the nights inside the period."""
    return sum(n for _r, n, _s in _booking_pieces(conn, property_id, start, end))


def reservation_count(conn, property_id, start, end):
    """Real reservations only, counted from the active source -- excludes the
    synthetic 'monthly-aggregate' rows the historical Excel import uses to carry
    a month's occupancy without a reconstructable reservation-level record."""
    return sum(1 for r, _n, _s in _booking_pieces(conn, property_id, start, end) if r["reservation_id"] != "monthly-aggregate")


def avg_stay(conn, property_id, start, end):
    """Average Length of Stay: nights on real reservations / real
    reservations (aggregate nights excluded on both sides)."""
    real = [(r, n) for r, n, _s in _booking_pieces(conn, property_id, start, end) if r["reservation_id"] != "monthly-aggregate"]
    return sum(n for _r, n in real) / len(real) if real else 0.0


def _active_days(start, end, property_start):
    """Nights in [start, end) on or after the date the property joined the portfolio (no start date = always)."""
    s, e = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    if property_start:
        s = max(s, datetime.date.fromisoformat(property_start))
    return max((e - s).days, 0)


def available_nights(conn, property_id, start, end):
    """Nights a property (or the portfolio) could have been booked. A property is only available from its start_date:
    before it joined the portfolio it is NOT ACTIVE, not "empty"."""
    if property_id:
        row = conn.execute("SELECT start_date FROM properties WHERE id=?", (property_id,)).fetchone()
        return _active_days(start, end, row["start_date"] if row else None)
    return sum(_active_days(start, end, r["start_date"])
               for r in conn.execute("SELECT start_date FROM properties WHERE type='flat' AND active=1"))


def occupancy(conn, property_id, start, end):
    avail = available_nights(conn, property_id, start, end)
    return booked_nights(conn, property_id, start, end) / avail if avail else 0.0


def adr(conn, property_id, start, end):
    """Average Daily Rate: accommodation revenue / occupied nights."""
    nights = booked_nights(conn, property_id, start, end)
    return accommodation_revenue(conn, property_id, start, end) / nights if nights else 0.0


def revpar(conn, property_id, start, end):
    """Revenue per Available Night: accommodation revenue / available nights."""
    avail = available_nights(conn, property_id, start, end)
    return accommodation_revenue(conn, property_id, start, end) / avail if avail else 0.0


def business_income(conn, property_id, start, end):
    """What this business actually earns for the period -- not the same as
    net_profit(). Most flats are run under a management agreement: the
    business only keeps its management fee (a cut of revenue) and the rest
    belongs to the flat's owner. That fee is itself a real, already-recorded
    transaction (category='management_fee') for any month it's been entered
    -- and the true rate has changed over time for some flats -- so the
    recorded amount is used whenever one exists for the period; only a
    month with no recorded fee yet falls back to the estimate from
    properties.management_fee_pct (times revenue). For a fully-owned flat
    (no fee set), the business keeps the whole net profit. Portfolio-wide
    (property_id=None) sums this per flat rather than netting on the total,
    since owned and managed flats are computed differently."""
    if property_id:
        row = conn.execute("SELECT management_fee_pct, type FROM properties WHERE id=?", (property_id,)).fetchone()
        if not row or row["type"] == "overhead":
            return 0.0
        fee = row["management_fee_pct"]
        if fee:
            recorded = conn.execute(
                """SELECT COALESCE(SUM(amount),0) FROM transactions
                   WHERE property_id=? AND direction='expense' AND category='management_fee' AND date>=? AND date<?""",
                (property_id, start, end),
            ).fetchone()[0]
            if recorded:
                return recorded
            return revenue(conn, property_id, start, end) * fee / 100
        return net_profit(conn, property_id, start, end)
    total = 0.0
    for p in conn.execute("SELECT id FROM properties WHERE type='flat'"):
        total += business_income(conn, p["id"], start, end)
    return total


def adjusted_revenue(conn, property_id, start, end):
    """Top-line money this business is actually entitled to: full revenue
    for an owned flat, only its management fee for a managed one -- the
    same figure as business_income() (real recorded fee first, the stored
    percentage as a fallback estimate), since a managed flat has no further
    costs of its own to subtract. Portfolio-wide sums each flat's own share
    rather than scaling one combined total, since owned and managed flats
    aren't adjusted by the same factor."""
    if property_id:
        row = conn.execute("SELECT management_fee_pct FROM properties WHERE id=?", (property_id,)).fetchone()
        fee = row["management_fee_pct"] if row else None
        return business_income(conn, property_id, start, end) if fee else revenue(conn, property_id, start, end)
    return sum(adjusted_revenue(conn, p["id"], start, end) for p in conn.execute("SELECT id FROM properties WHERE type='flat'"))


def adjusted_kpi_snapshot(conn, property_id, start, end):
    """Same shape as kpi_snapshot(), but revenue/costs/net_profit/margin
    reflect only what this business actually earns (adjusted_revenue() and
    business_income()) instead of every pound that moved through a managed
    flat on its owner's behalf. occupancy/booked_nights/adr/revpar describe
    the flat's own operating performance and are unaffected by who owns
    it, so those stay exactly as kpi_snapshot() computes them. The shared
    overhead cost centre isn't a revenue property at all -- the fee/
    ownership model doesn't apply to it, so it passes through unchanged
    (its real costs must stay visible, not be zeroed out by the "managed
    flats bear their own costs" rule below)."""
    if property_id:
        row = conn.execute("SELECT type FROM properties WHERE id=?", (property_id,)).fetchone()
        if row and row["type"] == "overhead":
            return kpi_snapshot(conn, property_id, start, end)
    rev = adjusted_revenue(conn, property_id, start, end)
    net = business_income(conn, property_id, start, end)
    return {
        "revenue": rev, "costs": max(rev - net, 0.0), "net_profit": net,
        "margin": net / rev if rev else 0.0,
        "occupancy": occupancy(conn, property_id, start, end),
        "booked_nights": booked_nights(conn, property_id, start, end),
        "adr": adr(conn, property_id, start, end),
        "revpar": revpar(conn, property_id, start, end),
    }


def adjusted_monthly_series(conn, property_id):
    """The adjusted_kpi_snapshot() equivalent of monthly_series() -- powers
    the Overview/property charts and year-on-year tables so they match the
    adjusted Revenue/Net profit tiles above them, instead of one part of
    the page showing your income and another showing everyone's."""
    out = []
    for ym in months_with_data(conn, property_id):
        year, month = map(int, ym.split("-"))
        start, end = month_bounds(year, month)
        snap = adjusted_kpi_snapshot(conn, property_id, start, end)
        snap["year"], snap["month"], snap["ym"] = year, month, ym
        out.append(snap)
    return out


def kpi_snapshot(conn, property_id, start, end):
    rev = revenue(conn, property_id, start, end)
    cost = costs(conn, property_id, start, end)
    nights = booked_nights(conn, property_id, start, end)
    return {
        "revenue": rev, "costs": cost, "net_profit": rev - cost,
        "margin": (rev - cost) / rev if rev else 0.0,
        "occupancy": occupancy(conn, property_id, start, end),
        "booked_nights": nights,
        "adr": adr(conn, property_id, start, end),
        "revpar": revpar(conn, property_id, start, end),
    }


def months_with_data(conn, property_id):
    """Sorted ['YYYY-MM', ...] for every month that has at least one
    transaction or booking -- the whole point being that a future month
    with nothing in it just isn't in this list, so charts never draw a
    false zero for it."""
    clause, params = _prop_clause(property_id)
    tx = conn.execute(f"SELECT DISTINCT substr(date,1,7) m FROM transactions WHERE 1=1 {clause}", params).fetchall()
    bk = conn.execute(f"SELECT DISTINCT substr(check_in,1,7) m FROM bookings WHERE 1=1 {clause}", params).fetchall()
    return sorted({r["m"] for r in tx} | {r["m"] for r in bk})


def monthly_series(conn, property_id):
    """One kpi_snapshot() per month in months_with_data() -- the direct
    replacement for reading monthly_summary."""
    out = []
    for ym in months_with_data(conn, property_id):
        year, month = map(int, ym.split("-"))
        start, end = month_bounds(year, month)
        snap = kpi_snapshot(conn, property_id, start, end)
        snap["year"], snap["month"], snap["ym"] = year, month, ym
        out.append(snap)
    return out


def data_average(conn, property_id, months=None):
    """Average monthly revenue / net profit / occupancy % over the months
    up to the current period that actually have revenue -- optionally only
    the latest `months` of them. This is the starting suggestion for a
    target, not a stored number; None where there is nothing to average."""
    cur = "%04d-%02d" % current_period(conn)
    rows = [r for r in monthly_series(conn, property_id) if r["ym"] <= cur and r["revenue"] > 0]
    if months:
        rows = rows[-months:]
    if not rows:
        return None
    n = len(rows)
    return {
        "revenue": sum(r["revenue"] for r in rows) / n,
        "profit": sum(r["net_profit"] for r in rows) / n,
        "occupancy": sum(r["occupancy"] for r in rows) / n * 100,
        "months": n,
    }


def current_period(conn):
    """The latest month where at least half the active flats have real
    income -- see the note in the old app.py's current_year_month() for
    why this isn't simply today's calendar month (hand-updated ledger)."""
    active_count = conn.execute("SELECT COUNT(*) FROM properties WHERE active=1 AND type='flat'").fetchone()[0] or 1
    threshold = max(1, active_count // 2)
    candidates = {}
    for row in conn.execute("SELECT property_id, substr(date,1,7) ym FROM transactions WHERE direction='income'"):
        candidates.setdefault(row["ym"], set()).add(row["property_id"])
    for row in conn.execute("SELECT property_id, substr(check_in,1,7) ym FROM bookings WHERE status='confirmed'"):
        candidates.setdefault(row["ym"], set()).add(row["property_id"])
    qualifying = sorted(ym for ym, props in candidates.items() if len(props) >= threshold)
    if qualifying:
        year, month = map(int, qualifying[-1].split("-"))
        return year, month
    today = datetime.date.today()
    return today.year, today.month
