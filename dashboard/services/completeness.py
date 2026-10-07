"""Per-property data health. The monthly workbook import is the source of truth for a property-month; uploaded
documents are supporting evidence only and are never reported as missing. (Legacy per-document requirements are
kept in the database but no longer drive any status.)"""
import datetime
import json

DEFAULT_REQUIRED = ["booking_statement", "cleaning_invoice", "bank_statement"]
DEFAULT_OPTIONAL = ["amazon_order", "utility_bill"]

DOC_TYPE_LABELS = {
    "amazon_order": "Amazon / Temu", "cleaning_invoice": "Cleaning",
    "booking_statement": "Booking platform", "bank_statement": "Bank statement",
    "utility_bill": "Utilities", "other": "Other",
}


def seed_defaults(conn, property_id):
    """Called once per property (existing flats via db.ensure_schema's
    backfill, new ones at creation time) -- a flat's own requirements can
    be edited later without this ever running again."""
    existing = conn.execute(
        "SELECT 1 FROM property_data_requirements WHERE property_id=? LIMIT 1", (property_id,)
    ).fetchone()
    if existing:
        return
    for source_type in DEFAULT_REQUIRED:
        conn.execute(
            "INSERT INTO property_data_requirements (property_id, source_type, required) VALUES (?,?,1)",
            (property_id, source_type),
        )
    for source_type in DEFAULT_OPTIONAL:
        conn.execute(
            "INSERT INTO property_data_requirements (property_id, source_type, required) VALUES (?,?,0)",
            (property_id, source_type),
        )


def not_active(conn, property_id, start, end):
    """Why nothing is expected of a property in [start, end), or None when it is expected to produce data. Three different things:
      not started  the WHOLE period is before its start date            -> "Not active until <date>"
      ended        the whole period is after its recorded end date      -> "Inactive"
      inactive     it is inactive now (and no end date is recorded)     -> "Inactive": it is no longer expected to send anything,
                   so nothing is "missing" (its historical figures are untouched and still shown where they exist)."""
    row = conn.execute("SELECT start_date, end_date, active FROM properties WHERE id=?", (property_id,)).fetchone()
    if not row:
        return None
    if row["start_date"] and row["start_date"] >= end:
        return {"kind": "not_started", "text": f"Not active until {row['start_date']}", "starts": row["start_date"]}
    if row["end_date"] and row["end_date"] < start:
        return {"kind": "inactive", "text": "Inactive", "starts": None}
    if not row["active"]:
        return {"kind": "inactive", "text": "Inactive", "starts": None}
    return None


def _inactive_health(why):
    return {"rows": [], "missing": [], "required_total": 0, "pct": 100, "has_data": False, "not_active": True,
            "starts": why["starts"], "kind": why["kind"], "text": why["text"]}


def has_real_data(conn, property_id, start, end):
    """Whether this property has any actual recorded transaction or
    booking for [start, end) -- independent of whether a *document* was
    uploaded for it. A month can have real data from a manual correction,
    a direct entry, or a document uploaded in a different month covering
    this period, so "no document uploaded this month" must never be read
    as "no data exists" -- that's a separate, narrower question answered
    by completeness_for()/health_for() below."""
    return bool(conn.execute(
        """SELECT 1 FROM transactions WHERE property_id=? AND date>=? AND date<?
           UNION SELECT 1 FROM bookings WHERE property_id=? AND check_in<? AND check_out>? LIMIT 1""",
        (property_id, start, end, property_id, end, start),
    ).fetchone())


def completeness_for(conn, property_id, start, end):
    """{'required': [...], 'received': [...], 'missing': [...], 'pct': float}
    for the [start, end) period -- 'received' checks whether a document of
    that source_type was uploaded during the period, same convention the
    Overview insights use (uploaded_at, not a detected/back-dated period).
    This is about *source documents*, not whether the property has real
    operating data -- see has_real_data() for that."""
    why = not_active(conn, property_id, start, end)
    if why:
        return {"required": [], "received": [], "missing": [], "pct": 100, "not_active": True, "starts": why["starts"], "kind": why["kind"], "text": why["text"]}
    reqs = conn.execute(
        "SELECT source_type, required FROM property_data_requirements WHERE property_id=?", (property_id,)
    ).fetchall()
    if not reqs:
        return None  # not configured -- don't imply a completeness figure that isn't real
    received_types = {r["doc_type"] for r in conn.execute(
        "SELECT DISTINCT doc_type FROM documents WHERE property_id=? AND uploaded_at>=? AND uploaded_at<?",
        (property_id, start, end),
    )}
    required = [r["source_type"] for r in reqs if r["required"]]
    missing = [t for t in required if t not in received_types]
    pct = round((len(required) - len(missing)) / len(required) * 100) if required else 100
    return {"required": required, "received": list(received_types), "missing": missing, "pct": pct}


def _months_in(start, end):
    """['YYYY-MM', ...] for every calendar month touched by [start, end)."""
    y, m = int(start[:4]), int(start[5:7])
    last = datetime.date.fromisoformat(end) - datetime.timedelta(days=1)
    out = []
    while (y, m) <= (last.year, last.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def workbook_cover(conn, property_id, start, end):
    """Which months of [start, end) were imported for this property from the monthly workbook (applied, not undone).
    The workbook is the source of truth for a month's figures; uploaded documents are only supporting evidence."""
    months = _months_in(start, end)
    covered, batches = [], []
    for b in conn.execute("SELECT id, period, properties FROM import_batches WHERE kind='workbook' AND status='applied' AND period IS NOT NULL ORDER BY id"):
        if b["period"] in months and property_id in json.loads(b["properties"] or "[]"):
            if b["period"] not in covered:
                covered.append(b["period"])
            batches.append(b["id"])
    return {"months": months, "covered": sorted(covered), "batches": batches}


def health_for(conn, property_id, start, end):
    """Where this property's figures for [start, end) come from. The monthly workbook import is the source of truth;
    uploaded documents are supporting evidence only and are never "missing". has_data is the broader signal: any real
    transaction/booking for the period at all."""
    why = not_active(conn, property_id, start, end)
    if why:
        return _inactive_health(why)
    cover = workbook_cover(conn, property_id, start, end)
    docs = conn.execute(
        "SELECT id, filename, doc_type, status FROM documents WHERE property_id=? AND uploaded_at>=? AND uploaded_at<? ORDER BY uploaded_at",
        (property_id, start, end),
    ).fetchall()
    rows = [{"source": d["doc_type"], "label": DOC_TYPE_LABELS.get(d["doc_type"], d["doc_type"]), "required": False, "received": True, "docs": [d]} for d in docs]
    full = bool(cover["months"]) and len(cover["covered"]) == len(cover["months"])
    return {"rows": rows, "missing": [], "required_total": 0, "pct": 100 if full else 0, "workbook": cover,
            "has_data": has_real_data(conn, property_id, start, end)}


def health_state(h, period_label):
    """(pill kind, human wording) for a property-period: imported from the monthly workbook, partly, an earlier
    (pre-workbook) import, or nothing yet. Supporting documents never make a period "partial"."""
    if h is None:
        return "neutral", "Not set up"
    if h.get("not_active"):
        return "neutral", h.get("text") or f"Not active until {h['starts']}"
    wb = h.get("workbook") or {"months": [], "covered": []}
    if wb["months"] and len(wb["covered"]) == len(wb["months"]):
        return "pos", "Imported from workbook"
    if wb["covered"]:
        return "neutral", f"Workbook · {len(wb['covered'])} of {len(wb['months'])} months"
    if h.get("has_data"):
        return "neutral", "Earlier import"
    return "neutral", f"Not imported yet"
