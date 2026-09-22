"""
Extracts structured transaction line items from an uploaded document -- an
Amazon/Temu order, a cleaner's invoice, a booking-platform statement, a
bank statement, a receipt -- using Claude for PDF/image, and a direct
(no-LLM, deterministic) parser for CSV/XLSX.

Mirrors this repo's existing "works with zero setup" pattern: with no
ANTHROPIC_API_KEY, extract() returns None for PDF/image and the app falls
back to a blank manual-entry form instead of crashing. CSV/XLSX parsing
never needs a key.

extract() returns {"items": [...], "document_hint": str|None,
"period_hint": "YYYY-MM"|None} or None. Each item:
{date, vendor, description, amount (always positive), category, confidence}.
"""
import base64
import csv
import json
import mimetypes
import os
import re
from pathlib import Path

EXTRACTION_PROMPT = """You are looking at a document from a UK short-term-rental \
business: it could be an Amazon/Temu order confirmation, a cleaner's invoice, \
a utility bill, an Airbnb/booking-platform payout statement, or a bank statement \
(which can mix several of the above in one file, one transaction per line).

Return ONLY a JSON object (no prose, no markdown fences) shaped like:
{
  "document_hint": "any address, property name or nickname literally printed on \
this document that might identify which flat it belongs to (e.g. a delivery \
address, a listing name) -- null if nothing like that appears",
  "period_hint": "YYYY-MM for the month this document covers, or null if unclear",
  "items": [
    {"date": "YYYY-MM-DD or null if unclear", "vendor": "string", \
"description": "short string", "amount": number, ALWAYS POSITIVE -- a plain \
magnitude, never negative, "category": one of "booking_income" (money the \
business received from a guest/booking platform), "purchase", "cleaning", \
"utilities", "rent", "management_fee" (a cut paid to whoever manages the flat), \
"other" (anything else the business paid out), \
"page": integer, the page of the document this line is printed on (1 if a single page), \
"confidence": number from 0 to 1, how sure you are this line is read correctly}
  ]
}

Only extract what's actually printed on the document -- never invent a vendor, \
amount or date that isn't there. If the document has one single total rather \
than line items, return that as a single item. On a bank statement, skip \
anything that's clearly a personal transaction rather than this business's."""


RESERVATION_PROMPT = """You are looking at a payout/earnings/reservation statement from a \
short-term-rental booking platform (Airbnb, Booking.com, Vrbo or similar) for a UK \
property business. It lists individual guest reservations, possibly for several flats.

Return ONLY a JSON object (no prose, no markdown fences) shaped like:
{
  "document_hint": "the listing/property name(s) printed on the statement, or null",
  "period_hint": "YYYY-MM for the month this statement covers, or null",
  "platform": "airbnb" | "booking_com" | "vrbo" | "other",
  "items": [
    {"reservation_id": "the confirmation/reservation code, or null",
     "description": "the listing/property name this reservation was for, or null",
     "check_in": "YYYY-MM-DD", "check_out": "YYYY-MM-DD",
     "gross": number (guest total before platform fees, or null),
     "fees": number (platform/host/service fees deducted, or 0),
     "net": number (what the host actually receives for this reservation),
     "page": integer, the page of the statement this reservation is printed on,
     "confidence": number from 0 to 1}
  ]
}

One item per reservation -- skip payout-summary lines, adjustments without dates, \
and totals. Never invent a date or amount that isn't printed. All amounts positive."""


def _parse_date(value):
    """Best-effort date -> 'YYYY-MM-DD'. Day-first for ambiguous d/m/y,
    since this is a UK business exporting UK-locale statements."""
    import datetime
    if value is None:
        return None
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d %B %Y", "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(text[:20] if fmt != "%Y-%m-%d" else text[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _num(value):
    if value is None:
        return None
    raw = str(value).replace(",", "").replace("£", "").replace("$", "").replace("€", "").strip()
    if raw in ("", "-"):
        return None
    neg = raw.startswith("(") and raw.endswith(")")
    try:
        n = float(raw.strip("()"))
    except ValueError:
        return None
    return -n if neg else n


_RES_COLUMN_HINTS = {
    "reservation_id": ("confirmation code", "confirmation", "reservation number", "reservation id", "reservation", "booking number", "booking id", "book number"),
    "check_in": ("start date", "check-in", "check in", "checkin", "arrival", "arriving"),
    "check_out": ("end date", "check-out", "check out", "checkout", "departure"),
    "nights": ("nights",),
    "listing": ("listing", "property", "accommodation", "unit", "apartment"),
    "gross": ("gross earnings", "gross", "total price", "price", "reservation amount"),
    "fees": ("service fee", "host fee", "commission", "platform fee", "fee"),
    "net": ("paid out", "net", "payout", "host earnings", "amount paid", "earnings"),
    "type": ("type",),
}


def _guess_reservation_columns(header):
    lower = [str(h or "").strip().lower() for h in header]
    found = {}
    for field, hints in _RES_COLUMN_HINTS.items():
        for hint in hints:  # earlier hints are more specific -- try them first
            idx = next((i for i, col in enumerate(lower) if hint in col and i not in found.values()), None)
            if idx is not None:
                found[field] = idx
                break
    return found


def _rows_to_reservations(header, rows):
    import datetime
    cols = _guess_reservation_columns(header)
    if "check_in" not in cols or not ({"net", "gross"} & set(cols)):
        return None  # can't be read as a reservation statement
    items = []
    for row_no, row in enumerate(rows, start=2):
        def cell(field):
            i = cols.get(field)
            return row[i] if i is not None and i < len(row) else None
        row_type = str(cell("type") or "").lower()
        if row_type and "reserv" not in row_type and "booking" not in row_type:
            continue  # payout / adjustment / tax lines
        check_in = _parse_date(cell("check_in"))
        if not check_in:
            continue
        check_out = _parse_date(cell("check_out"))
        nights = _num(cell("nights"))
        if not check_out and nights:
            check_out = (datetime.date.fromisoformat(check_in) + datetime.timedelta(days=int(nights))).isoformat()
        if not check_out:
            continue
        gross, fees, net = _num(cell("gross")), _num(cell("fees")), _num(cell("net"))
        if net is None and gross is not None:
            net = gross - abs(fees or 0)
        if net is None:
            continue
        items.append({"reservation_id": (str(cell("reservation_id")).strip() or None) if cell("reservation_id") else None,
                      "description": (str(cell("listing")).strip() or None) if cell("listing") else None,
                      "check_in": check_in, "check_out": check_out,
                      "gross": abs(gross) if gross is not None else abs(net),
                      "fees": abs(fees or 0), "net": abs(net), "confidence": 0.9 if (check_out and net is not None and cell("reservation_id")) else 0.7,
                      "row": row_no})
    return items


def _load_key():
    """The Anthropic key from the environment or the project's .env file."""
    from services.env import get
    return get("ANTHROPIC_API_KEY")


def available():
    return bool(_load_key())


def extract(file_path, mime_type=None, doc_type=None):
    """doc_type == 'booking_statement' means "read reservations" (each
    item is a real booking with check-in/out); anything else reads plain
    money-in/money-out transaction lines."""
    mime_type = mime_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    name = str(file_path).lower()
    reservations = doc_type == "booking_statement"
    prompt = RESERVATION_PROMPT if reservations else EXTRACTION_PROMPT
    if name.endswith((".xlsx", ".xlsm")) or mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return _extract_xlsx(file_path, reservations, doc_type)
    if name.endswith(".xls"):
        return _extract_xls(file_path, reservations, doc_type)
    if name.endswith((".csv", ".tsv")) or mime_type in ("text/csv", "text/tab-separated-values"):
        return _extract_csv(file_path, reservations, doc_type)
    if name.endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif")) or mime_type == "application/pdf" or (mime_type or "").startswith("image/"):
        return _extract_via_claude(file_path, mime_type, prompt, reservations)
    # Anything else (txt, docx, json, eml, html...): a delimited text file
    # can still be read as a table; otherwise hand the text to the model.
    text = read_text(file_path)
    if text is None:
        return None
    if name.endswith(".txt"):
        tabular = _extract_csv(file_path, reservations, doc_type)
        if tabular:
            return tabular
    return _extract_via_claude(file_path, mime_type, prompt, reservations, text=text)


def read_text(file_path):
    """Plain text of a text-like file (docx paragraphs included), or None
    for binary content we can't read."""
    path = str(file_path)
    try:
        if path.lower().endswith(".docx"):
            import zipfile
            xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8", errors="replace")
            xml = re.sub(r"</w:p>", "\n", xml)
            xml = re.sub(r"<w:tab/>", "\t", xml)
            return re.sub(r"<[^>]+>", "", xml).strip() or None
        raw = open(path, "rb").read(2_000_000)
    except Exception:
        return None
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8-sig", errors="replace").strip() or None


def _extract_via_claude(file_path, mime_type, prompt=None, reservations=False, text=None):
    prompt = prompt or EXTRACTION_PROMPT
    api_key = _load_key()
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    if text is not None:
        content_block = {"type": "text", "text": "DOCUMENT TEXT:\n" + text[:60000]}
    else:
        data = base64.standard_b64encode(open(file_path, "rb").read()).decode()
        if mime_type == "application/pdf":
            content_block = {"type": "document", "source": {"type": "base64", "media_type": mime_type, "data": data}}
        elif mime_type and mime_type.startswith("image/"):
            content_block = {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": data}}
        else:
            return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=4096 if reservations else 2048,
            messages=[{"role": "user", "content": [content_block, {"type": "text", "text": prompt}]}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[1] if "\n" in text else text
        result = json.loads(text)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            return None
        return {
            "items": result["items"],
            "document_hint": result.get("document_hint"),
            "period_hint": result.get("period_hint"),
            "kind": "reservation" if reservations else "transaction",
            "platform": result.get("platform"),
        }
    except Exception:
        return None


_COLUMN_HINTS = {
    "date": ("date",),
    "vendor": ("vendor", "payee", "merchant", "description 1", "name"),
    "description": ("description", "memo", "details", "narrative"),
    "amount": ("amount", "value", "total", "debit", "credit"),
}


def _guess_columns(header):
    lower = [h.strip().lower() for h in header]
    found = {}
    for field, hints in _COLUMN_HINTS.items():
        for i, col in enumerate(lower):
            if any(h in col for h in hints):
                found[field] = i
                break
    return found


_DOC_TYPE_CATEGORY = {"amazon_order": "purchase", "cleaning_invoice": "cleaning", "utility_bill": "utilities"}


def _rows_to_items(header, rows, doc_type=None):
    cols = _guess_columns(header)
    if "amount" not in cols:
        return None  # can't make sense of this sheet without an amount column
    items = []
    for row_no, row in enumerate(rows, start=2):
        if not row or cols["amount"] >= len(row):
            continue
        raw_amount = str(row[cols["amount"]]).replace(",", "").replace("£", "").strip()
        if not raw_amount:
            continue
        try:
            amount = float(raw_amount)
        except ValueError:
            continue
        if amount == 0:
            continue
        category = "booking_income" if amount > 0 and "date" not in cols else ("purchase" if amount < 0 else "other")
        items.append({
            "date": _parse_date(row[cols["date"]]) if "date" in cols and cols["date"] < len(row) else None,
            "vendor": row[cols["vendor"]] if "vendor" in cols and cols["vendor"] < len(row) else None,
            "description": row[cols["description"]] if "description" in cols and cols["description"] < len(row) else None,
            "amount": abs(amount),
            "category": _DOC_TYPE_CATEGORY.get(doc_type) or ("booking_income" if amount > 0 else "other"),
            # column-guessing, not a verified read: confident only when the sheet has a
            # date and a vendor or description column alongside the amount
            "confidence": 0.9 if ("date" in cols and ("vendor" in cols or "description" in cols)) else 0.6,
            "row": row_no,
        })
    return items


def _extract_csv(file_path, reservations=False, doc_type=None):
    with open(file_path, newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = "\t" if str(file_path).lower().endswith(".tsv") else _sniff_delimiter(sample)
        reader = csv.reader(f, delimiter=delimiter)
        rows = list(reader)
    if not rows:
        return None
    if reservations:
        items = _rows_to_reservations(rows[0], rows[1:])
        return {"items": items, "document_hint": None, "period_hint": None, "kind": "reservation", "platform": None} if items else None
    items = _rows_to_items(rows[0], rows[1:], doc_type)
    if items is None:
        return None
    return {"items": items, "document_hint": None, "period_hint": None, "kind": "transaction"}


def _sniff_delimiter(sample):
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _extract_xls(file_path, reservations=False, doc_type=None):
    """Legacy .xls -- needs the optional xlrd package; without it the file
    is still saved and can be entered by hand."""
    try:
        import xlrd
    except ImportError:
        return None
    try:
        ws = xlrd.open_workbook(file_path).sheet_by_index(0)
    except Exception:
        return None
    rows = [[ws.cell_value(r, c) for c in range(ws.ncols)] for r in range(ws.nrows)]
    return _sheet_result(rows, reservations, doc_type)


def _sheet_result(rows, reservations, doc_type=None):
    if not rows:
        return None
    header = [str(c) if c is not None else "" for c in rows[0]]
    if reservations:
        items = _rows_to_reservations(header, rows[1:])
        return {"items": items, "document_hint": None, "period_hint": None, "kind": "reservation", "platform": None} if items else None
    items = _rows_to_items(header, rows[1:], doc_type)
    if items is None:
        return None
    return {"items": items, "document_hint": None, "period_hint": None, "kind": "transaction"}


def _extract_xlsx(file_path, reservations=False, doc_type=None):
    try:
        import openpyxl
    except ImportError:
        return None
    wb = openpyxl.load_workbook(file_path, data_only=True)
    ws = wb.active
    rows = [[c.value for c in row] for row in ws.iter_rows()]
    if not rows:
        return None
    header = [str(c) if c is not None else "" for c in rows[0]]
    if reservations:
        items = _rows_to_reservations(header, rows[1:])
        return {"items": items, "document_hint": None, "period_hint": None, "kind": "reservation", "platform": None} if items else None
    items = _rows_to_items(header, rows[1:], doc_type)
    if items is None:
        return None
    return {"items": items, "document_hint": None, "period_hint": None, "kind": "transaction"}


_NON_WORD = re.compile(r"[^a-z0-9]+")


def guess_property(hint_text, properties):
    """Fuzzy-matches free text (from document_hint, or a filename) against
    known property names/addresses. Returns (property_id, confidence) or
    (None, 0) -- confidence is just "how much of the property's own name
    matched", not a calibrated probability."""
    if not hint_text:
        return None, 0.0
    hint = _NON_WORD.sub(" ", hint_text.lower())
    best_id, best_score = None, 0.0
    for p in properties:
        for candidate in (p["name"], p["address"] or ""):
            words = [w for w in _NON_WORD.sub(" ", candidate.lower()).split() if len(w) > 2]
            if not words:
                continue
            hits = sum(1 for w in words if w in hint)
            score = hits / len(words)
            if score > best_score:
                best_id, best_score = p["id"], score
    return (best_id, round(best_score, 2)) if best_score >= 0.5 else (None, 0.0)
