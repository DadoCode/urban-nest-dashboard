"""Which booking data feeds the KPIs, decided per PROPERTY and MONTH.

Two kinds of booking data can describe the same month:

  legacy_aggregate  the Excel history: a lump of booking income (transactions,
                    source='excel_import', direction='income') plus a zero-value
                    "monthly-aggregate" booking row that carries the nights.
  detailed          real reservations added later (source 'upload'/'manual'/'ical').

They overlap, but not line by line, so adding them together double counts and
letting any one reservation replace the month destroys it. So exactly ONE
source feeds a property-month, chosen explicitly:

  * a row in booking_source_state says which one (legacy_aggregate | detailed);
  * with no row: legacy_aggregate if the month has Excel data, else detailed
    (there is nothing to protect in a month Excel doesn't cover);
  * confirming a document NEVER writes a row -- only a person does, from the
    reconciliation screen, after seeing the projected impact.

The Excel rows are never modified, so reverting restores the old figures exactly.
Every KPI that touches bookings asks `Sources` which source owns each month.
"""
import contextlib
import datetime
import json
import threading

# Rows written from the monthly workbook (and the older Excel history that came
# from the same workbook) are the aggregate side; nothing else is.
AGGREGATE_SOURCES = ("excel_import", "workbook")

LEGACY = "legacy_aggregate"
DETAILED = "detailed"
STATES = (LEGACY, DETAILED)

# Reservations that came from a statement, hand entry or a synced calendar.
# (Excel's synthetic 'monthly-aggregate' rows are the legacy side.)
def is_detailed_row(row):
    return row["source"] not in AGGREGATE_SOURCES


def _ym(text):
    return text[:7]


def _month_start(ym):
    return datetime.date(int(ym[:4]), int(ym[5:7]), 1)


def _next_month(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def months_between(start, end):
    """Every 'YYYY-MM' that [start, end) touches (ISO date strings)."""
    s, e = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    if e <= s:
        return []
    out, ym = [], _ym(start)
    last = _ym((e - datetime.timedelta(days=1)).isoformat())
    while ym <= last:
        out.append(ym)
        ym = _next_month(ym)
    return out


_local = threading.local()


@contextlib.contextmanager
def forced(mapping):
    """Temporarily answer "which source?" for given property-months -- used ONLY to
    compute what-if figures with the very same KPI code the dashboard runs
    (projected impact, existing-vs-uploaded). Never persisted."""
    previous = getattr(_local, "force", None)
    _local.force = {**(previous or {}), **mapping}
    try:
        yield
    finally:
        _local.force = previous


class Sources:
    """A snapshot of the data needed to answer "which source owns this
    property-month?" for a date range -- fetched in a few queries, so a KPI
    call never loops over the database month by month."""

    def __init__(self, conn, property_id, start, end):
        self.start, self.end = start, end
        clause, params = ("AND property_id=?", (property_id,)) if property_id else ("", ())
        first, last = _ym(start), _ym((datetime.date.fromisoformat(end) - datetime.timedelta(days=1)).isoformat())
        self.explicit = {(r["property_id"], r["month"]): r["active_source"] for r in conn.execute(
            f"SELECT property_id, month, active_source FROM booking_source_state WHERE month>=? AND month<=? {clause}", (first, last, *params))}
        # Excel data present for a property-month. Judged over WHOLE calendar months, so a range that
        # starts or ends mid-month (month-to-date, "next 30 days") still sees that month's Excel history.
        lo, hi = f"{first}-01", f"{_next_month(last)}-01"
        self.legacy_months = {(r[0], r[1]) for r in conn.execute(
            f"""SELECT DISTINCT property_id, substr(date,1,7) FROM transactions
                WHERE source IN ('excel_import','workbook') AND direction='income' AND date>=? AND date<? {clause}""", (lo, hi, *params))}
        self.legacy_months |= {(r[0], r[1]) for r in conn.execute(
            f"""SELECT DISTINCT property_id, substr(check_in,1,7) FROM bookings
                WHERE reservation_id='monthly-aggregate' AND status='confirmed' AND check_in>=? AND check_in<? {clause}""", (lo, hi, *params))}

    def active(self, property_id, ym):
        what_if = getattr(_local, "force", None)
        if what_if and (property_id, ym) in what_if:
            return what_if[(property_id, ym)]
        chosen = self.explicit.get((property_id, ym))
        if chosen:
            return chosen
        return LEGACY if (property_id, ym) in self.legacy_months else DETAILED


def active_source(conn, property_id, ym):
    """The source owning one property-month, and whether a person chose it."""
    row = conn.execute("SELECT active_source FROM booking_source_state WHERE property_id=? AND month=?", (property_id, ym)).fetchone()
    if row:
        return row["active_source"], True
    s = f"{ym}-01"
    e = f"{_next_month(ym)}-01"
    return Sources(conn, property_id, s, e).active(property_id, ym), False


def dedupe_detailed(rows):
    """One row per real reservation: the same confirmation code (or, with no
    code, the same dates and amount) from two documents counts once -- the
    most recently stored copy wins."""
    seen, out = {}, []
    for r in sorted(rows, key=lambda r: -r["id"]):
        key = (r["property_id"], r["reservation_id"]) if r["reservation_id"] else (r["property_id"], r["check_in"], r["check_out"], round(r["net_revenue"] or 0, 2))
        if key in seen:
            continue
        seen[key] = True
        out.append(r)
    return out


def prorate(row, lo, hi, value):
    """`value` scaled to the nights of the stay that fall inside [lo, hi)."""
    ci, co = datetime.date.fromisoformat(row["check_in"]), datetime.date.fromisoformat(row["check_out"])
    span = (co - ci).days
    inside = (min(co, hi) - max(ci, lo)).days
    return (value * inside / span) if (span > 0 and inside > 0) else 0.0


def nights_inside(row, lo, hi):
    ci, co = datetime.date.fromisoformat(row["check_in"]), datetime.date.fromisoformat(row["check_out"])
    return max((min(co, hi) - max(ci, lo)).days, 0)


def stay_pieces(row, start, end):
    """(ym, lo, hi) for each calendar month a stay overlaps inside [start, end)."""
    s, e = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    ci, co = datetime.date.fromisoformat(row["check_in"]), datetime.date.fromisoformat(row["check_out"])
    for ym in months_between(max(ci, s).isoformat(), min(co, e).isoformat()):
        ms = _month_start(ym)
        me = _month_start(_next_month(ym))
        lo, hi = max(ms, ci, s), min(me, co, e)
        if hi > lo:
            yield ym, lo, hi


# ---------------------------------------------------------------- decisions

def set_source(conn, property_id, ym, new_state, user="owner", note=None):
    """Record an explicit decision (never done by an import). Returns the previous
    effective state so the change can be reported and reverted."""
    if new_state not in STATES:
        raise ValueError(new_state)
    old, explicit = active_source(conn, property_id, ym)
    if explicit and old == new_state:
        return old          # already decided this way: nothing to write, nothing to audit
    conn.execute(
        """INSERT INTO booking_source_state (property_id, month, active_source, decided_at, decided_by, note)
           VALUES (?,?,?,datetime('now'),?,?)
           ON CONFLICT(property_id, month) DO UPDATE SET active_source=excluded.active_source,
             decided_at=excluded.decided_at, decided_by=excluded.decided_by, note=excluded.note""",
        (property_id, ym, new_state, user, note))
    conn.execute("INSERT INTO audit_log (entity_type, entity_id, action, field, old_value, new_value, user) VALUES ('booking_source',?,?,?,?,?,?)",
                 (f"{property_id}|{ym}", "select_source", "active_source", old, new_state, user))
    return old
