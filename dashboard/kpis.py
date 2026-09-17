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


def _prop_clause(property_id, column="property_id"):
    return (f"AND {column} = ?", (property_id,)) if property_id else ("", ())


def revenue(conn, property_id, start, end):
    """All accommodation + ancillary income for the period."""
    clause, params = _prop_clause(property_id)
    tx = conn.execute(
        f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE direction='income' AND date>=? AND date<? {clause}",
        (start, end, *params),
    ).fetchone()[0]
    bk = conn.execute(
        f"SELECT COALESCE(SUM(net_revenue),0) FROM bookings WHERE status='confirmed' AND check_in<? AND check_out>? {clause}",
        (end, start, *params),
    ).fetchone()[0]
    return tx + bk


def accommodation_revenue(conn, property_id, start, end):
    """Revenue narrowed to actual stays (booking_income transactions +
    real bookings) -- excludes ancillary/other income -- for ADR/RevPAR."""
    clause, params = _prop_clause(property_id)
    tx = conn.execute(
        f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE direction='income' AND category='booking_income' AND date>=? AND date<? {clause}",
        (start, end, *params),
    ).fetchone()[0]
    bk = conn.execute(
        f"SELECT COALESCE(SUM(net_revenue),0) FROM bookings WHERE status='confirmed' AND check_in<? AND check_out>? {clause}",
        (end, start, *params),
    ).fetchone()[0]
    return tx + bk


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
        f"SELECT check_in, check_out FROM bookings WHERE status='confirmed' AND check_in<? AND check_out>? {clause}",
        (end, start, *params),
    ).fetchall()
    start_d, end_d = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    total = 0
    for r in rows:
        ci = max(datetime.date.fromisoformat(r["check_in"]), start_d)
        co = min(datetime.date.fromisoformat(r["check_out"]), end_d)
        total += max((co - ci).days, 0)
    return total


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
