"""Shared helpers used by more than one route module -- property lookups,
percentage-delta math, and the month-tile builders every page's KPI row
is assembled from. Nothing here talks to Flask (no request/response) --
it's plain data access and arithmetic over a connection."""
import services.kpis as kpis

MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

CATEGORIES = ["purchase", "cleaning", "utilities", "rent", "deposit", "council_tax", "maintenance", "management_fee",
              "insurance", "software", "accounting", "marketing", "salary", "furniture", "booking_income", "other"]

# One-sentence definitions for the handful of labels that are easy to
# misread -- not a replacement for a clear name, just precision on top
# of one. Keyed by the same `key` every KPI tile already carries, so a
# tile picks its own definition up automatically (see kpi_tile() in
# _components.html); referenced directly by `title=` where a plain
# table header wants the same wording without the visible icon.
METRIC_INFO = {
    "gross_booking_revenue": "Total guest booking value for reservations in this period -- not the same as Urban Nest Revenue. A managed property's booking value mostly belongs to its owner; Urban Nest's own income from it is Management Fee Earned.",
    "revenue": "Revenue Urban Nest itself earns in the selected period: full property revenue for operated properties, plus management fees earned from managed properties.",
    "fee": "The management fee Urban Nest earns for managing the property, based on the agreed percentage of its booking revenue.",
    "net_profit": "Revenue minus this property's own costs. For a managed property, this equals Management Fee Earned, since Urban Nest's own income from it is just the fee.",
    "adr": "Average nightly rate actually achieved on booked nights.",
    "revpar": "Revenue per available night (occupancy × ADR) -- how hard a property is working.",
    # Targets' own calculation is NOT business-model-aware (see
    # routes/targets.py) -- it's gross revenue minus this property's own
    # costs for every property the same way, including a managed
    # property's management-fee expense. That's a different number from
    # the adjusted "Property Profit" shown on Overview/Properties, so it
    # gets its own distinct name rather than reusing that one.
    "property_operating_profit": "Gross booking revenue minus this property's own operating costs, before company-level business expenses. For a managed property this reflects the property's own economics, not Urban Nest's management income -- see Management Fee Earned for that.",
}


def pct_delta(current, previous, min_base=0):
    """None when there's nothing to compare against, or when `previous` is
    too small (below min_base) for a percentage off it to mean anything --
    a swing off a near-zero base is noise, not signal (e.g. "+8490%" when
    last year's figure was a few pounds), so it's omitted rather than
    shown literally or capped."""
    if not previous or abs(previous) < min_base:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def get_properties(conn, active_only=True, include_overhead=True):
    q = "SELECT * FROM properties"
    clauses = []
    if active_only:
        clauses.append("active = 1")
    if not include_overhead:
        clauses.append("type != 'overhead'")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY (type = 'overhead'), name"
    return conn.execute(q).fetchall()


def get_property(conn, property_id):
    return conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()


def yoy_pairs(conn, property_id, current_period):
    """[(label, this_year, last_year, delta_pct), ...] for every month up to
    current_period where the same month exists a year earlier too."""
    series = {(s["year"], s["month"]): s for s in kpis.monthly_series(conn, property_id)}
    pairs = []
    for (year, month), row in sorted(series.items()):
        if (year, month) > current_period:
            continue
        prev = series.get((year - 1, month))
        if prev:
            pairs.append({
                "label": f"{MONTH_NAMES[month]} {year}",
                "this_year": row["revenue"],
                "last_year": prev["revenue"],
                "delta_pct": pct_delta(row["revenue"], prev["revenue"], min_base=100),
            })
    return pairs


def adjusted_yoy_pairs(conn, property_id, current_period):
    """yoy_pairs(), but built from adjusted_monthly_series() -- revenue here
    means what this business actually earns, matching the adjusted Revenue
    tile rather than the flat's full gross revenue."""
    series = {(s["year"], s["month"]): s for s in kpis.adjusted_monthly_series(conn, property_id)}
    pairs = []
    for (year, month), row in sorted(series.items()):
        if (year, month) > current_period:
            continue
        prev = series.get((year - 1, month))
        if prev:
            pairs.append({
                "label": f"{MONTH_NAMES[month]} {year}",
                "this_year": row["revenue"],
                "last_year": prev["revenue"],
                "delta_pct": pct_delta(row["revenue"], prev["revenue"], min_base=100),
            })
    return pairs


def tiles_for(conn, property_id, year, month):
    py, pm = kpis.prior_month(year, month)
    start, end = kpis.month_bounds(year, month)
    pstart, pend = kpis.month_bounds(py, pm)
    cur = kpis.kpi_snapshot(conn, property_id, start, end)
    prev = kpis.kpi_snapshot(conn, property_id, pstart, pend)
    tiles = [
        {"label": f"Revenue — {MONTH_NAMES[month]} {year}", "value": f"£{cur['revenue']:,.0f}",
         "delta": pct_delta(cur["revenue"], prev["revenue"], min_base=100)},
        {"label": "Net profit", "value": f"£{cur['net_profit']:,.0f}",
         "delta": pct_delta(cur["net_profit"], prev["net_profit"], min_base=300)},
        {"label": "Occupancy", "value": f"{cur['occupancy'] * 100:.0f}%",
         "delta": pct_delta(cur["occupancy"], prev["occupancy"], min_base=0.05)},
        {"label": "Booked nights", "value": cur["booked_nights"],
         "delta": pct_delta(cur["booked_nights"], prev["booked_nights"], min_base=2)},
    ]
    return tiles, cur, prev


CHANNELS = {"airbnb": "Airbnb", "booking": "Booking.com", "direct": "Direct", "vrbo": "Vrbo", "other": "Other"}


def channel_key(platform):
    """Collapses the free-text platform on a booking to one of CHANNELS."""
    p = (platform or "").lower().replace(".", "").replace("_", "").replace(" ", "")
    if "airbnb" in p:
        return "airbnb"
    if "booking" in p:
        return "booking"
    if "direct" in p:
        return "direct"
    if "vrbo" in p or "homeaway" in p:
        return "vrbo"
    return "other"
