"""Support for the document-ingestion workflow: a lightweight lifecycle
trail, file hashing for duplicate detection, property/period detection
with explicit uncertainty, and a before/after KPI snapshot.

Nothing here changes a financial figure -- it only records what the parser
decided, what the reviewer changed, and which records a confirmation
created, so a wrong number can be traced back to its source.
"""
import hashlib
import json
import re
from collections import Counter

import services.kpis as kpis

MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Statuses stored in documents.status -> (label shown to the user, pill kind).
# 'extracted' and the legacy 'reviewed' both mean "waiting on you".
STATUS_DISPLAY = {
    "pending": ("Uploaded", "neutral"),
    "processing": ("Processing", "neutral"),
    "extracted": ("Needs review", "warn"),
    "reviewed": ("Needs review", "warn"),
    "confirmed": ("Confirmed", "pos"),
    "failed": ("Failed", "neg"),
}

# Filter keys on the Documents page -> the stored statuses they cover.
STATUS_FILTERS = {
    "uploaded": ("pending",),
    "processing": ("processing",),
    "review": ("extracted", "reviewed"),
    "confirmed": ("confirmed",),
    "failed": ("failed",),
}
STATUS_FILTER_LABELS = {"uploaded": "Uploaded", "processing": "Processing", "review": "Needs review",
                        "confirmed": "Confirmed", "failed": "Failed"}
STATUS_FILTER_ALIASES = {"complete": "confirmed"}  # older links used d_status=complete

# Full document-type names (the upload dropdown and the review page);
# services.completeness has the short forms used by the health panel.
DOC_TYPE_NAMES = {
    "amazon_order": "Amazon / Temu order", "cleaning_invoice": "Cleaning invoice",
    "booking_statement": "Booking / Airbnb statement", "bank_statement": "Bank statement",
    "utility_bill": "Utility bill", "other": "Other",
}

EVENT_LABELS = {
    "uploaded": "Uploaded", "extracted": "Extracted", "extraction_failed": "Extraction failed",
    "edited": "Edited", "confirmed": "Confirmed", "undone": "Import undone",
}


def status_display(status):
    return STATUS_DISPLAY.get(status, ("Needs review", "warn"))


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- lifecycle trail ----------------------------------------------------

def log_event(conn, document_id, event, summary, detail=None):
    conn.execute(
        "INSERT INTO document_events (document_id, event, summary, detail) VALUES (?,?,?,?)",
        (document_id, event, summary, json.dumps(detail) if detail is not None else None))


def events_for(conn, document_id):
    out = []
    for r in conn.execute("SELECT * FROM document_events WHERE document_id=? ORDER BY id", (document_id,)):
        try:
            detail = json.loads(r["detail"]) if r["detail"] else None
        except ValueError:
            detail = None
        out.append({"event": r["event"], "label": EVENT_LABELS.get(r["event"], r["event"]),
                    "summary": r["summary"], "detail": detail, "timestamp": r["timestamp"]})
    return out


def detection_of(doc):
    try:
        return json.loads(doc["detection_json"]) if doc["detection_json"] else {}
    except (TypeError, ValueError):
        return {}


# ---- period / property detection ---------------------------------------

def _ym(text):
    return text[:7] if text and re.match(r"^\d{4}-\d{2}", text) else None


def period_label(period):
    """'Feb 2026', or 'Jan–Mar 2026' when the lines span several months."""
    if not period:
        return None
    a, b = period.get("from"), period.get("to")
    if not a:
        return None
    ay, am = map(int, a.split("-"))
    if not b or b == a:
        return f"{MONTH_ABBR[am]} {ay}"
    by, bm = map(int, b.split("-"))
    if ay == by:
        return f"{MONTH_ABBR[am]}–{MONTH_ABBR[bm]} {ay}"
    return f"{MONTH_ABBR[am]} {ay} – {MONTH_ABBR[bm]} {by}"


def detect_period(items, reservations, hint):
    """(period dict | None, warnings). Combines the document's own stated
    period (hint, 'YYYY-MM') with the months its lines are actually dated
    in, and says so when they disagree or the lines span several months."""
    warnings = []
    months = Counter(_ym(i.get("check_in") if reservations else i.get("date")) for i in items)
    months.pop(None, None)
    hint_ym = hint if hint and re.match(r"^\d{4}-\d{2}$", hint) else None
    if not months and not hint_ym:
        warnings.append({"code": "period_missing", "level": "warn",
                         "message": "No period could be detected — the lines carry no usable dates. Check the date on each line before confirming."})
        return None, warnings
    if not months:
        return {"from": hint_ym, "to": hint_ym, "source": "document", "ambiguous": False, "months": {}}, warnings
    ordered = sorted(months)
    dominant = max(ordered, key=lambda m: (months[m], -ordered.index(m)))
    period = {"from": ordered[0], "to": ordered[-1], "dominant": dominant, "months": dict(months),
              "source": "line dates", "ambiguous": len(ordered) > 1}
    if hint_ym:
        period["source"] = "document and line dates"
        outside = sum(n for m, n in months.items() if m != hint_ym)
        if outside / sum(months.values()) >= 0.5:
            period["ambiguous"] = True
            warnings.append({"code": "period_mismatch", "level": "warn",
                             "message": f"The document says {period_label({'from': hint_ym})} but most of its lines are dated {period_label({'from': dominant})}. Check which period is right."})
            return period, warnings
        period["dominant"] = hint_ym
    if len(ordered) > 1 and not any(w["code"] == "period_mismatch" for w in warnings):
        warnings.append({"code": "period_ambiguous", "level": "info",
                         "message": f"Lines span {period_label(period)}. Each line keeps its own date; the document's period is shown as the most common month."})
    return period, warnings


def rank_is_ambiguous(ranked):
    """Two different properties matching about equally well -- and the
    runner-up not being less specific (a full match on a longer name beats a
    full match on a shorter one contained in it)."""
    return len(ranked) > 1 and ranked[1]["score"] >= ranked[0]["score"] - 0.1 and ranked[1].get("hits", 0) >= ranked[0].get("hits", 0)


# ---- duplicate files ----------------------------------------------------

def find_duplicate_document(conn, doc_id, file_hash):
    """The earlier upload with the same bytes (a confirmed one preferred), or None."""
    if not file_hash:
        return None
    return conn.execute(
        """SELECT id, filename, status, uploaded_at FROM documents
           WHERE file_hash=? AND id != ? AND status != 'failed' ORDER BY (status='confirmed') DESC, id DESC LIMIT 1""",
        (file_hash, doc_id)).fetchone()


def find_similar_document(conn, doc_id, doc_type, property_id, year, month, n_rows, total):
    """A different file that is the same kind of document for the same
    property and period with the same row count and total -- likely a
    re-export of the same statement."""
    if not (property_id and year and month and n_rows):
        return None
    for d in conn.execute(
            """SELECT id, filename, status, uploaded_at FROM documents
               WHERE id != ? AND doc_type=? AND property_id=? AND detected_year=? AND detected_month=?
                 AND status IN ('extracted','reviewed','confirmed') ORDER BY id DESC""",
            (doc_id, doc_type, property_id, year, month)):
        row = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM document_items WHERE document_id=?", (d["id"],)).fetchone()
        if row["n"] == n_rows and abs(row["amt"] - total) < 0.01:
            return d
    return None


# ---- KPI before/after ---------------------------------------------------

def month_key(date_text):
    ym = _ym(date_text)
    return tuple(map(int, ym.split("-"))) if ym else None


def kpi_snapshot(conn, touched):
    """{(property_id, year, month): {revenue, costs, booked_nights}} read
    from the existing KPI functions for each property-month a confirmation
    is about to touch (or just touched). Raw P&L figures -- the numbers the
    ledger itself produces, not the adjusted Urban Nest views."""
    out = {}
    for pid, y, m in touched:
        s, e = kpis.month_bounds(y, m)
        out[(pid, y, m)] = {"revenue": round(kpis.revenue(conn, pid, s, e), 2),
                            "costs": round(kpis.costs(conn, pid, s, e), 2),
                            "booked_nights": kpis.booked_nights(conn, pid, s, e)}
    return out


def kpi_changes(before, after, names):
    """Only the property-months where something actually moved, as plain rows."""
    rows = []
    for key in sorted(after, key=lambda k: (names.get(k[0], k[0]), k[1], k[2])):
        b, a = before.get(key, {}), after[key]
        deltas = {k: round(a[k] - b.get(k, 0), 2) for k in a}
        if any(abs(v) > 0.004 for v in deltas.values()):
            rows.append({"property": names.get(key[0], key[0]), "period": f"{MONTH_ABBR[key[2]]} {key[1]}",
                         "before": b, "after": a})
    return rows


# ---- source traceability ------------------------------------------------

def provenance(conn, document_id, line=None):
    """What a drawer shows under "Source": the document a record came from,
    and where in it (page/row) when the line is known. None if the record
    has no document (an Excel import or a hand-typed entry)."""
    from services.common import MONTH_NAMES
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone() if document_id else None
    if not doc:
        return None
    period = detection_of(doc).get("period")
    if period:
        period_text = period_label(period)
    elif doc["detected_year"] and doc["detected_month"]:
        period_text = f"{MONTH_NAMES[doc['detected_month']][:3]} {doc['detected_year']}"
    else:
        period_text = None
    prop = conn.execute("SELECT name FROM properties WHERE id=?", (doc["property_id"],)).fetchone() if doc["property_id"] else None
    label, kind = status_display(doc["status"])
    return {
        "id": doc["id"], "filename": doc["filename"], "type": DOC_TYPE_NAMES.get(doc["doc_type"], doc["doc_type"] or "Document"),
        "uploaded": doc["uploaded_at"][:16], "property": prop["name"] if prop else None, "period": period_text,
        "status": label, "kind": kind, "ref": f"D-{doc['id']:04d}",
        "page": line["source_page"] if line and line["source_page"] else None,
        "row": line["source_row"] if line and line["source_row"] else None,
        "line_index": line["line_index"] + 1 if line is not None else None,
        "line_id": line["id"] if line is not None else None,
    }


# ---- remembered listing -> property matches ------------------------------

def norm_alias(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def alias_lookup(conn, text):
    """{"property_id", "ignore"} for a listing name you've matched before, else None."""
    key = norm_alias(text)
    if not key:
        return None
    row = conn.execute("SELECT property_id, ignore FROM property_aliases WHERE alias=?", (key,)).fetchone()
    return {"property_id": row["property_id"], "ignore": bool(row["ignore"])} if row else None


def alias_remember(conn, text, property_id, ignore=False):
    key = norm_alias(text)
    if key and (property_id or ignore):
        conn.execute("INSERT OR REPLACE INTO property_aliases (alias, label, property_id, ignore) VALUES (?,?,?,?)",
                     (key, " ".join((text or "").split())[:200], None if ignore else property_id, 1 if ignore else 0))
