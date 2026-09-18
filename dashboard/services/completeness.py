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


def completeness_for(conn, property_id, start, end):
    """{'required': [...], 'received': [...], 'missing': [...], 'pct': float}
    for the [start, end) period -- 'received' checks whether a document of
    that source_type was uploaded during the period, same convention the
    Overview insights use (uploaded_at, not a detected/back-dated period)."""
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
