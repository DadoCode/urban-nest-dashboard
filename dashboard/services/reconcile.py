"""Existing-vs-uploaded comparison and the explicit source switch, per
property and month. Everything here is computed with the SAME KPI functions the
dashboard uses (services.kpis), under a what-if source (services.sources.forced),
so a projected figure can never differ from what the switch will really show."""
import services.kpis as kpis
import services.sources as src

MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def month_label(ym):
    return f"{MONTH_ABBR[int(ym[5:])]} {ym[:4]}"


def bounds(ym):
    return f"{ym}-01", f"{src._next_month(ym)}-01"


def metrics(conn, property_id, ym, source):
    """Booking + financial figures for one month as they would read if `source` owned it."""
    s, e = bounds(ym)
    with src.forced({(property_id, ym): source}):
        snap = kpis.adjusted_kpi_snapshot(conn, property_id, s, e)
        return {
            "gbr": kpis.accommodation_revenue(conn, property_id, s, e),
            "nights": kpis.booked_nights(conn, property_id, s, e),
            "reservations": kpis.reservation_count(conn, property_id, s, e),
            "occupancy": kpis.occupancy(conn, property_id, s, e),
            "adr": kpis.adr(conn, property_id, s, e),
            "revpar": kpis.revpar(conn, property_id, s, e),
            "un_revenue": snap["revenue"], "profit": snap["net_profit"],
        }


def _all_legacy_months(conn):
    out = {(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT property_id, substr(date,1,7) FROM transactions WHERE source='excel_import' AND direction='income'")}
    out |= {(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT property_id, substr(check_in,1,7) FROM bookings WHERE reservation_id='monthly-aggregate' AND status='confirmed'")}
    return out


def detailed_months(conn):
    """{(property_id, ym)} that have at least one stored (non-Excel) reservation night."""
    out = set()
    for r in conn.execute("SELECT property_id, check_in, check_out FROM bookings WHERE status='confirmed' AND source!='excel_import'"):
        for ym in src.months_between(r["check_in"], r["check_out"]):
            out.add((r["property_id"], ym))
    return out


def status_of(conn, property_id, ym, legacy=None, detailed=None):
    """{active, explicit, label, kind}. 'RECONCILIATION NEEDED' = Excel history AND stored
    reservations both exist for the month and nobody has decided which to use."""
    legacy = _all_legacy_months(conn) if legacy is None else legacy
    detailed = detailed_months(conn) if detailed is None else detailed
    active, explicit = src.active_source(conn, property_id, ym)
    has_legacy, has_detailed = (property_id, ym) in legacy, (property_id, ym) in detailed
    if explicit and active == src.DETAILED:
        label, kind = "Using detailed bookings", "pos"
    elif explicit:
        label, kind = "Excel history confirmed (uploads stored, not used)", "neutral"
    elif has_legacy and has_detailed:
        label, kind = "RECONCILIATION NEEDED", "warn"
    elif has_detailed:
        label, kind = "Detailed bookings (no Excel for this month)", "neutral"
    else:
        label, kind = "Excel history", "neutral"
    return {"active": active, "explicit": explicit, "label": label, "kind": kind, "has_legacy": has_legacy, "has_detailed": has_detailed}


def candidates(conn):
    """Property-months that have BOTH Excel history and stored reservations -- the ones that
    need a person's decision -- newest first, with their existing/uploaded figures."""
    legacy, detailed = _all_legacy_months(conn), detailed_months(conn)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM properties")}
    rows = []
    for pid, ym in sorted(legacy & detailed, key=lambda k: (k[1], names.get(k[0], k[0])), reverse=True):
        existing, uploaded = metrics(conn, pid, ym, src.LEGACY), metrics(conn, pid, ym, src.DETAILED)
        rows.append({"property_id": pid, "property": names.get(pid, pid), "ym": ym, "label_month": month_label(ym),
                     "existing": existing, "uploaded": uploaded, "status": status_of(conn, pid, ym, legacy, detailed),
                     "verdict": verdict(existing, uploaded)})
    return rows


def verdict(existing, uploaded):
    """A plain-words hint -- never an automatic decision."""
    if not existing["gbr"] and not existing["nights"]:
        return ("No Excel figure to compare", "neutral")
    ratio_r = uploaded["gbr"] / existing["gbr"] if existing["gbr"] else None
    ratio_n = uploaded["nights"] / existing["nights"] if existing["nights"] else None
    low = [r for r in (ratio_r, ratio_n) if r is not None and r < 0.9]
    if low:
        return (f"INCOMPLETE? uploaded is {min(low) * 100:.0f}% of Excel", "warn")
    high = [r for r in (ratio_r, ratio_n) if r is not None and r > 1.1]
    if high:
        return (f"Uploaded is {max(high) * 100:.0f}% of Excel (Excel may be incomplete, or stays overlap)", "warn")
    return ("Close to Excel (within 10%)", "pos")


def uploaded_detail(conn, property_id, ym):
    """The stored reservations touching this month, de-duplicated, with the documents they came from."""
    s, e = bounds(ym)
    rows = conn.execute(
        """SELECT b.id, b.property_id, b.platform, b.reservation_id, b.check_in, b.check_out, b.net_revenue, b.gross_revenue, b.source, b.document_id,
                  d.filename FROM bookings b LEFT JOIN documents d ON d.id=b.document_id
           WHERE b.property_id=? AND b.status='confirmed' AND b.source!='excel_import' AND b.check_in<? AND b.check_out>?
           ORDER BY b.check_in""", (property_id, e, s)).fetchall()
    rows = src.dedupe_detailed(rows)
    lo, hi = src.datetime.date.fromisoformat(s), src.datetime.date.fromisoformat(e)
    out, docs, gross_guest = [], {}, 0.0
    for r in sorted(rows, key=lambda r: r["check_in"]):
        n = src.nights_inside(r, lo, hi)
        gross_guest += src.prorate(r, lo, hi, r["gross_revenue"] or 0.0)
        out.append({**dict(r), "nights_in_month": n})
        if r["document_id"]:
            docs[r["document_id"]] = r["filename"]
    return {"rows": out, "documents": [{"id": k, "filename": v} for k, v in docs.items()], "guest_gross": gross_guest,
            "days_in_month": (hi - lo).days}


def trail(conn, property_id, ym):
    return conn.execute("SELECT timestamp, old_value, new_value, user FROM audit_log WHERE entity_type='booking_source' AND entity_id=? ORDER BY id",
                        (f"{property_id}|{ym}",)).fetchall()


NICE = {src.LEGACY: "Excel history", src.DETAILED: "Detailed bookings"}


def describe_trail(rows):
    """'Excel history -> Detailed bookings -> Excel history' as readable steps."""
    return [{"when": r["timestamp"][:16], "text": f"{NICE.get(r['old_value'], r['old_value'])} → {NICE.get(r['new_value'], r['new_value'])}",
             "who": r["user"] or ""} for r in rows]


def needed_count(conn):
    """How many property-months have Excel history AND stored reservations but no decision yet."""
    legacy, detailed = _all_legacy_months(conn), detailed_months(conn)
    decided = {(r[0], r[1]) for r in conn.execute("SELECT property_id, month FROM booking_source_state")}
    return len((legacy & detailed) - decided)
