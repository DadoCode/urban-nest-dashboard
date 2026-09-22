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



# Real reservations (uploaded from a booking statement, or entered by hand)
# supersede the Excel import's monthly figures for that flat and month:
# the import carries that month's income as lump booking_income entries plus a
# synthetic "monthly-aggregate" booking row for its nights, so counting both
# the lump and the real reservations would double the month. Derived on
# read, nothing is deleted -- remove the reservations and the Excel figures
# for that month come straight back.
_REAL_RES = ("rb.status='confirmed' AND rb.reservation_id != 'monthly-aggregate' "
             "AND rb.source IN ('upload','manual')")
_TX_NOT_SUPERSEDED = (
    "AND NOT (transactions.source='excel_import' AND transactions.direction='income' AND EXISTS ("
    "SELECT 1 FROM bookings rb WHERE rb.property_id = transactions.property_id AND " + _REAL_RES +
    " AND substr(rb.check_in,1,7) = substr(transactions.date,1,7)))")
_BK_NOT_SUPERSEDED = (
    "AND NOT (bookings.reservation_id='monthly-aggregate' AND EXISTS ("
    "SELECT 1 FROM bookings rb WHERE rb.property_id = bookings.property_id AND " + _REAL_RES +
    " AND substr(rb.check_in,1,7) = substr(bookings.check_in,1,7)))")

def _booking_revenue(conn, clause, params, start, end):
    """Reservation income falling inside [start, end): a stay that straddles
    a month boundary is prorated by nights, so it isn't counted in full in
    both months."""
    rows = conn.execute(
        f"SELECT check_in, check_out, net_revenue FROM bookings WHERE status='confirmed' AND check_in<? AND check_out>? {clause}",
        (end, start, *params),
    ).fetchall()
    start_d, end_d = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    total = 0.0
    for r in rows:
        ci_full, co_full = datetime.date.fromisoformat(r["check_in"]), datetime.date.fromisoformat(r["check_out"])
        span = (co_full - ci_full).days
        inside = (min(co_full, end_d) - max(ci_full, start_d)).days
        if span > 0 and inside > 0:
            total += r["net_revenue"] * inside / span
    return total


def revenue(conn, property_id, start, end):
    """All accommodation + ancillary income for the period."""
    clause, params = _prop_clause(property_id)
    tx = conn.execute(
        f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE direction='income' AND date>=? AND date<? {clause} {_TX_NOT_SUPERSEDED}",
        (start, end, *params),
    ).fetchone()[0]
    return tx + _booking_revenue(conn, clause, params, start, end)


def accommodation_revenue(conn, property_id, start, end):
    """Revenue narrowed to actual stays (booking_income transactions +
    real bookings) -- excludes ancillary/other income -- for ADR/RevPAR."""
    clause, params = _prop_clause(property_id)
    tx = conn.execute(
        f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE direction='income' AND category='booking_income' AND date>=? AND date<? {clause} {_TX_NOT_SUPERSEDED}",
        (start, end, *params),
    ).fetchone()[0]
    return tx + _booking_revenue(conn, clause, params, start, end)


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
    """Sums nights from every booking clipped to [start, end) -- a
    reservation spanning the boundary only contributes the nights that
    actually fall inside the requested period."""
    clause, params = _prop_clause(property_id)
    rows = conn.execute(
        f"SELECT check_in, check_out FROM bookings WHERE status='confirmed' AND check_in<? AND check_out>? {clause} {_BK_NOT_SUPERSEDED}",
        (end, start, *params),
    ).fetchall()
    start_d, end_d = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    total = 0
    for r in rows:
        ci = max(datetime.date.fromisoformat(r["check_in"]), start_d)
        co = min(datetime.date.fromisoformat(r["check_out"]), end_d)
        total += max((co - ci).days, 0)
    return total


def reservation_count(conn, property_id, start, end):
    """Real reservations only -- excludes the synthetic 'monthly-aggregate'
    rows the historical Excel import uses to carry a month's occupancy
    figure without a reconstructable reservation-level record."""
    clause, params = _prop_clause(property_id)
    return conn.execute(
        f"""SELECT COUNT(*) FROM bookings WHERE status='confirmed' AND reservation_id != 'monthly-aggregate'
            AND check_in<? AND check_out>? {clause}""",
        (end, start, *params),
    ).fetchone()[0]


def avg_stay(conn, property_id, start, end):
    """Average Length of Stay: nights on real reservations / real
    reservations. Only real reservations on both sides -- dividing all
    booked nights (which include the Excel import's aggregate nights) by
    the count of real ones would be meaningless."""
    clause, params = _prop_clause(property_id)
    rows = conn.execute(
        f"""SELECT check_in, check_out FROM bookings WHERE status='confirmed' AND reservation_id != 'monthly-aggregate'
            AND check_in<? AND check_out>? {clause}""",
        (end, start, *params),
    ).fetchall()
    if not rows:
        return 0.0
    start_d, end_d = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    nights = sum(max((min(datetime.date.fromisoformat(r["check_out"]), end_d)
                      - max(datetime.date.fromisoformat(r["check_in"]), start_d)).days, 0) for r in rows)
    return nights / len(rows)


def available_nights(conn, property_id, start, end):
    days = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days
    if property_id:
        return days
    n = conn.execute("SELECT COUNT(*) FROM properties WHERE type='flat' AND active=1").fetchone()[0]
    return days * n


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
    business only keeps a percentage of revenue (properties.management_fee_pct)
    and the rest belongs to the flat's owner. For a fully-owned flat
    (no fee set), the business keeps the whole net profit. Portfolio-wide
    (property_id=None) sums this per flat rather than netting on the total,
    since owned and managed flats are computed differently."""
    if property_id:
        row = conn.execute("SELECT management_fee_pct, type FROM properties WHERE id=?", (property_id,)).fetchone()
        if not row or row["type"] == "overhead":
            return 0.0
        fee = row["management_fee_pct"]
        if fee:
            return revenue(conn, property_id, start, end) * fee / 100
        return net_profit(conn, property_id, start, end)
    total = 0.0
    for p in conn.execute("SELECT id FROM properties WHERE type='flat'"):
        total += business_income(conn, p["id"], start, end)
    return total


def adjusted_revenue(conn, property_id, start, end):
    """Top-line money this business is actually entitled to: full revenue
    for an owned flat, only the management-fee share of revenue for a
    managed one (properties.management_fee_pct). Portfolio-wide sums each
    flat's own share rather than scaling one combined total, since owned
    and managed flats aren't adjusted by the same factor."""
    if property_id:
        row = conn.execute("SELECT management_fee_pct FROM properties WHERE id=?", (property_id,)).fetchone()
        rev = revenue(conn, property_id, start, end)
        fee = row["management_fee_pct"] if row else None
        return rev * fee / 100 if fee else rev
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
