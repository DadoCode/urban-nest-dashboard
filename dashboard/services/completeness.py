"""Per-property data completeness -- judged against what *that* property is
actually expected to produce (property_data_requirements), not one
hardcoded checklist every flat gets measured against regardless of which
platforms/vendors it actually uses. See the V2 plan's amendment on this:
"92% complete" is only meaningful once the requirement itself is real."""

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


def health_for(conn, property_id, start, end):
    """Every source this property is expected to produce, and whether each
    one has arrived for [start, end) -- the itemised version of
    completeness_for(), for the "what exactly is missing?" views. Uses the
    same rule as completeness_for: a document of that type uploaded in the
    period counts as received. has_data is the separate, broader signal:
    does this property have any real transaction/booking for the period
    at all, regardless of documents."""
    reqs = conn.execute(
        "SELECT source_type, required FROM property_data_requirements WHERE property_id=? ORDER BY required DESC, rowid",
        (property_id,),
    ).fetchall()
    if not reqs:
        return None
    docs = conn.execute(
        "SELECT id, filename, doc_type, status FROM documents WHERE property_id=? AND uploaded_at>=? AND uploaded_at<? ORDER BY uploaded_at",
        (property_id, start, end),
    ).fetchall()
    rows = []
    for r in reqs:
        mine = [d for d in docs if d["doc_type"] == r["source_type"]]
        rows.append({"source": r["source_type"], "label": DOC_TYPE_LABELS.get(r["source_type"], r["source_type"]),
                     "required": bool(r["required"]), "received": bool(mine), "docs": mine})
    required = [x for x in rows if x["required"]]
    missing = [x for x in required if not x["received"]]
    pct = round((len(required) - len(missing)) / len(required) * 100) if required else 100
    return {"rows": rows, "missing": missing, "required_total": len(required), "pct": pct,
            "has_data": has_real_data(conn, property_id, start, end)}


def health_state(h, period_label):
    """(pill kind, human wording) for a health summary -- no vague words.
    "No {period} data" is reserved for when there's genuinely nothing
    recorded for the property that period; a property with real data but
    missing source documents is "Partial", never "No data", even if every
    expected document happens to be missing."""
    if h is None:
        return "neutral", "Not set up"
    if h["pct"] == 100:
        return "pos", "Complete"
    n = len(h["missing"])
    if not h.get("has_data") and h["required_total"] and n == h["required_total"]:
        return "neutral", f"No {period_label} data"
    return "warn", f"Partial · {n} source{'s' if n != 1 else ''} missing"
