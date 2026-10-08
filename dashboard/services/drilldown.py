"""The records behind a property's headline numbers, for one period.

Read-only. Every total here is built from the SAME rows and the SAME source rules the KPI functions use (services/kpis.py
and services/sources.py), so the number you click is the number you see added up. Nothing is recalculated with a new formula:
the KPI value is returned next to the breakdown so a test (and the page) can show they agree."""
import datetime

import services.kpis as kpis
import services.sources as src
from services.common import is_managed
from services.provenance import is_workbook_row, review_status, workbook_source


def _ym_range(start, end):
    last = datetime.date.fromisoformat(end) - datetime.timedelta(days=1)
    return start[:7], last.strftime("%Y-%m")


def _source_cell(conn, row):
    wb = workbook_source(conn, row)
    if wb and wb["kind"] == "workbook":
        return {"kind": "workbook", "batch_id": wb["batch_id"], "ref": wb["ref"], "anchor": wb["anchor"], "label": wb["label"]}
    if wb:
        return {"kind": "legacy"}
    return {"kind": "manual"}


def revenue_records(conn, prop, start, end):
    """Income rows and (for a managed property) management-fee rows that make up this property's figures.

    gross_booking_revenue  = booking-income rows + reservation-level bookings   (kpis.accommodation_revenue)
    urban_nest_revenue     = gross_booking_revenue + other income               (kpis.revenue; an operated property's own revenue)
    management_fee         = recorded fee rows, else the percentage estimate     (kpis.business_income)
    """
    pid = prop["id"]
    S = src.Sources(conn, pid, start, end)
    income = []
    for r in conn.execute("SELECT * FROM transactions WHERE property_id=? AND direction='income' AND date>=? AND date<? ORDER BY date, id", (pid, start, end)):
        if r["source"] in src.AGGREGATE_SOURCES and S.active(pid, r["date"][:7]) == src.DETAILED:
            continue                                                  # superseded by detailed reservations for that month (same rule as kpis._income)
        income.append({"id": r["id"], "date": r["date"], "booking": r["category"] == "booking_income",
                       "kind": "Booking income" if r["category"] == "booking_income" else "Other income",
                       "text": r["description"] or r["vendor"] or "", "amount": r["amount"], "src": _source_cell(conn, r)})
    booking_income = sum(i["amount"] for i in income if i["booking"])
    other_income = sum(i["amount"] for i in income if not i["booking"])
    pieces = kpis._booking_pieces(conn, pid, start, end)
    reservations = sum(share for _r, _n, share in pieces)
    gross = booking_income + reservations

    managed = is_managed(prop)
    fee = None
    if managed:
        rows = []
        for r in conn.execute("SELECT * FROM transactions WHERE property_id=? AND direction='expense' AND category='management_fee' AND date>=? AND date<? ORDER BY date, id",
                              (pid, start, end)):
            rows.append({"id": r["id"], "date": r["date"], "text": r["description"] or "Management fee", "amount": r["amount"], "src": _source_cell(conn, r)})
        recorded = sum(x["amount"] for x in rows)
        value = kpis.business_income(conn, pid, start, end)
        pct = prop["management_fee_pct"]
        fee = {"rows": rows, "recorded": recorded, "value": value, "pct": pct, "estimated": not rows and bool(value),
               "rate": (recorded / gross * 100) if rows and gross else None}
    lo, hi = _ym_range(start, end)
    review = review_status(conn, pid, lo, hi)
    return {"income": income, "booking_income": booking_income, "other_income": other_income, "reservations": reservations,
            "gross": gross, "urban_nest_revenue": gross + other_income,
            "kpi_gross": kpis.accommodation_revenue(conn, pid, start, end), "kpi_revenue": kpis.revenue(conn, pid, start, end),
            "fee": fee, "managed": managed, "review": review}


def nights_evidence(conn, prop, start, end):
    """The booked nights that make up occupancy, ADR and RevPAR: reservation rows where individual reservations exist,
    otherwise the month's recorded total (workbook 'Days Booked' or the earlier Excel monthly total)."""
    pid = prop["id"]
    items = []
    for r, nights, share in kpis._booking_pieces(conn, pid, start, end):
        full = conn.execute("SELECT * FROM bookings WHERE id=?", (r["id"],)).fetchone()
        aggregate = r["reservation_id"] == "monthly-aggregate"
        items.append({"id": r["id"], "aggregate": aggregate, "check_in": r["check_in"], "check_out": r["check_out"], "nights": nights,
                      "src": _source_cell(conn, full) if full else {"kind": "manual"}, "net": share})
    items.sort(key=lambda i: (i["check_in"], i["id"]))
    booked = sum(i["nights"] for i in items)
    return {"rows": items, "booked": booked, "kpi_booked": kpis.booked_nights(conn, pid, start, end),
            "available": kpis.available_nights(conn, pid, start, end), "occupancy": kpis.occupancy(conn, pid, start, end),
            "gross": kpis.accommodation_revenue(conn, pid, start, end), "adr": kpis.adr(conn, pid, start, end),
            "revpar": kpis.revpar(conn, pid, start, end), "monthly_only": bool(items) and all(i["aggregate"] for i in items)}
