"""Shared helpers used by more than one route module -- property lookups,
percentage-delta math, and the month-tile builders every page's KPI row
is assembled from. Nothing here talks to Flask (no request/response) --
it's plain data access and arithmetic over a connection."""
import services.kpis as kpis

MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

CATEGORIES = ["purchase", "cleaning", "utilities", "rent", "maintenance", "management_fee",
              "insurance", "software", "accounting", "furniture", "booking_income", "other"]


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
