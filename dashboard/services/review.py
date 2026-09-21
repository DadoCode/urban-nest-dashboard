"""Helpers for the two-pane document review workspace: what the reviewer
needs to see next to each extracted line (possible duplicates, which
values they've changed, where in the source file it came from) and a
plain-table preview of CSV/XLSX sources so they can be checked in place."""
import csv
import datetime
import json
import re

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# extracted-JSON key -> document_items column, for "manually corrected" counts
_TX_FIELDS = {"vendor": "vendor", "description": "raw_description", "amount": "amount", "category": "category", "date": "date"}
_RES_FIELDS = {"check_in": "check_in", "check_out": "check_out", "gross": "gross_revenue", "fees": "platform_fees",
               "net": "net_revenue", "reservation_id": "reservation_id"}


def _short_date(iso):
    try:
        d = datetime.date.fromisoformat(iso)
        return f"{d.day} {d.strftime('%b')}"
    except (TypeError, ValueError):
        return iso or ""


def original_of(item):
    try:
        return json.loads(item["original_extracted_value"] or "{}")
    except (TypeError, ValueError):
        return {}


def changed_fields(item):
    """Names of the fields whose current value no longer matches what was
    extracted. Empty for hand-entered lines (nothing to compare against)."""
    orig = original_of(item)
    if not orig:
        return []
    fields = _RES_FIELDS if item["item_kind"] == "reservation" else _TX_FIELDS
    out = []
    for key, col in fields.items():
        a, b = orig.get(key), item[col]
        if a in (None, "") and b in (None, ""):
            continue
        try:
            same = abs(float(a) - float(b)) < 0.005
        except (TypeError, ValueError):
            same = str(a or "").strip() == str(b or "").strip()
        if not same:
            out.append(key)
    return out


def duplicate_info(conn, item, find_duplicate_reservation):
    """The existing record this line looks like, or None. Only meaningful
    before the document is confirmed."""
    if item["item_kind"] == "reservation":
        booking_id = find_duplicate_reservation(conn, item["property_id"], {
            "reservation_id": item["reservation_id"], "check_in": item["check_in"], "check_out": item["check_out"]})
        if not booking_id:
            return None
        b = conn.execute("SELECT b.*, p.name pname FROM bookings b JOIN properties p ON p.id=b.property_id WHERE b.id=?", (booking_id,)).fetchone()
        if not b:
            return None
        return {"kind": "reservation", "id": b["id"], "property_id": b["property_id"],
                "text": f"{b['platform'] or 'Reservation'} · {_short_date(b['check_in'])}–{_short_date(b['check_out'])} · £{b['net_revenue']:,.2f}",
                "property": b["pname"]}
    if not item["duplicate_of"]:
        return None
    t = conn.execute("SELECT t.*, p.name pname FROM transactions t JOIN properties p ON p.id=t.property_id WHERE t.id=?", (item["duplicate_of"],)).fetchone()
    if not t:
        return None
    return {"kind": "transaction", "id": t["id"], "property_id": t["property_id"],
            "text": f"{t['vendor'] or t['description'] or 'Transaction'} · £{t['amount']:,.2f} · {_short_date(t['date'])}",
            "property": t["pname"]}


def file_preview(path, limit=300, max_cols=14):
    """Rows of a CSV/XLSX source as plain strings, header first, so a
    reviewer can see the file next to what was read from it. None for
    anything else. Row N in this list is row N of the file."""
    p = str(path).lower()
    try:
        if p.endswith((".csv", ".tsv")):
            with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
                sample = f.read(4096)
                f.seek(0)
                delim = "\t" if p.endswith(".tsv") else _delimiter(sample)
                rows = [r for _, r in zip(range(limit), csv.reader(f, delimiter=delim))]
        elif p.endswith((".xlsx", ".xlsm")):
            import openpyxl
            ws = openpyxl.load_workbook(path, data_only=True, read_only=True).active
            rows = [["" if c is None else str(c) for c in r] for _, r in zip(range(limit), ws.iter_rows(values_only=True))]
        else:
            import services.extraction as extraction
            text = extraction.read_text(path)
            if text is None:
                return None
            rows = [[line] for line in text.splitlines()[:limit]]
    except Exception:
        return None
    return [[str(c) for c in r[:max_cols]] for r in rows]


def _delimiter(sample):
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","
