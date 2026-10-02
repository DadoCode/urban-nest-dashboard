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
import logging
import mimetypes
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

# The extraction API rejects images over 5 MB; checking first lets us say so
# plainly instead of surfacing an opaque API error.
IMAGE_API_LIMIT = 5 * 1024 * 1024

NO_KEY_MESSAGE = "Automatic extraction isn't configured. You can enter the extracted rows manually."


class ExtractionFailure(Exception):
    """A known, explainable reason a file could not be read automatically.
    `message` is written for the person uploading, never for a log."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message

EXTRACTION_PROMPT = """You are looking at a document from a UK short-term-rental \
business: it could be an Amazon/Temu order confirmation, a cleaner's invoice, \
a utility bill, an Airbnb/booking-platform payout statement, or a bank statement \
(which can mix several of the above in one file, one transaction per line).

Return ONLY a JSON object (no prose, no markdown fences) shaped like:
{
  "document_hint": "any address, property name or nickname literally printed on \
this document that might identify which flat it belongs to (e.g. a delivery \
address, a listing name) — null if nothing like that appears",
  "period_hint": "YYYY-MM for the month this document covers, or null if unclear",
  "items": [
    {"date": "YYYY-MM-DD or null if unclear", "vendor": "string", \
"description": "short string", "amount": number, ALWAYS POSITIVE — a plain \
magnitude, never negative, "category": one of "booking_income" (money the \
business received from a guest/booking platform), "purchase", "cleaning", \
"utilities", "rent", "council_tax", "management_fee" (a cut paid to whoever \
manages the flat), "insurance", "software", "accounting", "marketing", \
"salary", "furniture", "other" (anything else the business paid out), \
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


_NUMERIC_DATE = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")


def _detect_day_first(values):
    """Which order a column of numeric dates is written in: True = day/month,
    False = month/day, None = can't tell. Decided by the evidence in the
    whole column (any first part over 12 means day-first; any second part
    over 12 means month-first) -- never by guessing one cell at a time, which
    silently turns 09/03/2026 into 9 March in a US-format export."""
    dmy = mdy = False
    for v in values:
        m = _NUMERIC_DATE.match(str(v or ""))
        if not m:
            continue
        first, second = int(m.group(1)), int(m.group(2))
        dmy = dmy or first > 12
        mdy = mdy or second > 12
    if dmy and not mdy:
        return True
    if mdy and not dmy:
        return False
    return None


def _is_ambiguous_date(value):
    m = _NUMERIC_DATE.match(str(value or ""))
    return bool(m) and int(m.group(1)) <= 12 and int(m.group(2)) <= 12 and m.group(1) != m.group(2)


def _parse_date(value, day_first=True):
    """Best-effort date -> 'YYYY-MM-DD'. Ambiguous d/m/y defaults to
    day-first (a UK business), unless the caller worked out from the whole
    column that it is month-first."""
    import datetime
    if value is None:
        return None
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return None
    dmy = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")
    mdy = ("%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y")
    preferred, fallback = (dmy, mdy) if day_first else (mdy, dmy)
    for fmt in ("%Y-%m-%d", *preferred, "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d %B %Y", *fallback):
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


def _h(text):
    """A header or hint with every difference that doesn't matter removed:
    case, tabs/newlines/non-breaking spaces, punctuation and hyphens
    ("Arrival\t" == "arrival", "Check-in" == "check in")."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


_RES_COLUMN_HINTS = {
    "reservation_id": ("confirmation code", "confirmation", "reservation number", "reservation id", "reservation", "booking number", "booking id", "book number"),
    "check_in": ("start date", "check-in", "check in", "checkin", "arrival", "arriving"),
    "check_out": ("end date", "check-out", "check out", "checkout", "departure"),
    "nights": ("nights",),
    "listing": ("listing", "property name", "property", "accommodation", "unit", "apartment"),
    "location": ("location", "address"),
    "gross": ("gross earnings", "gross", "total payment", "total price", "price", "reservation amount"),
    "fees": ("service fee", "host fee", "commission", "platform fee", "fee"),
    "net": ("paid out", "net", "payout", "host earnings", "amount paid", "earnings"),
    "amount": ("amount",),
    "type": ("type",),
    "status": ("status",),
    "currency": ("currency",),
}


def _guess_reservation_columns(header):
    lower = [_h(h) for h in header]
    found = {}
    for field, hints in _RES_COLUMN_HINTS.items():
        for hint in hints:  # earlier hints are more specific -- try them first
            idx = next((i for i, col in enumerate(lower) if _h(hint) in col and i not in found.values()), None)
            if idx is not None:
                found[field] = idx
                break
    return found


def _clean_code(value):
    """A reservation code as text: spreadsheets turn 5200756724 into 5200756724.0."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text or None


def _platform_of(header):
    """Which booking platform an export is from, judged by its column names."""
    lower = {_h(h) for h in header}
    if {"confirmation code", "gross earnings"} <= lower or "airbnb remitted tax" in lower:
        return "airbnb"
    if {"reservation number", "booker name"} <= lower or "genius booker" in lower:
        return "booking_com"
    return None


_OK_STATUSES = {"ok", "confirmed", "booked", "completed", "complete", "checked in", "checked out", "stayed", "finished", "reservation", "accepted"}


def _money(n):
    return f"{'-' if n < 0 else ''}£{abs(n):,.2f}"


def _rows_to_reservations(header, rows):
    """-> (items | None, notes, rows_tried). Reads real reservations only and
    says what it left out, so a missing amount can be traced to a decision
    rather than a mystery."""
    import datetime
    from collections import Counter, defaultdict
    cols = _guess_reservation_columns(header)
    if "check_in" not in cols or not ({"net", "gross", "amount"} & set(cols)):
        return None, [], 0  # can't be read as a reservation statement

    def raw(row, field):
        i = cols.get(field)
        return row[i] if i is not None and i < len(row) else None

    # every date-like column votes (the statement's own Date and Booking date columns settle the order just as well as the stay dates)
    date_cols = {cols["check_in"], *([cols["check_out"]] if "check_out" in cols else []), *(i for i, h in enumerate(header) if "date" in str(h or "").lower())}
    day_first = _detect_day_first([r[i] for r in rows for i in date_cols if i < len(r)])
    items, notes = [], []
    left_out, left_out_amt = Counter(), defaultdict(float)
    cancelled = tried = bad_dates = night_mismatch = ambiguous = 0
    odd_status, currencies, statuses = Counter(), Counter(), Counter()
    seen, dup_codes = set(), []
    for row_no, row in enumerate(rows, start=2):
        if not any(str(c or "").strip() for c in row):
            continue
        row_type = str(raw(row, "type") or "").strip()
        low = row_type.lower()
        if row_type and "reserv" not in low and "booking" not in low:
            if low != "payout":  # bank transfers are not reservations or adjustments; stay silent about them
                left_out[row_type] += 1
                left_out_amt[row_type] += _num(raw(row, "amount")) or _num(raw(row, "net")) or 0
            continue
        status_raw = str(raw(row, "status") or "").strip()
        status = _h(status_raw)
        exclude_why = None
        if status:
            if "cancel" in status or "no show" in status or "noshow" in status:
                exclude_why = f"status \u201c{status_raw}\u201d (cancelled / no-show)"
                cancelled += 1
            elif status not in _OK_STATUSES:
                exclude_why = f"unrecognised status \u201c{status_raw}\u201d"
                odd_status[status_raw] += 1
        tried += 1
        nights = _num(raw(row, "nights"))
        ci_raw, co_raw = raw(row, "check_in"), raw(row, "check_out")
        if day_first is not None:
            check_in, check_out = _parse_date(ci_raw, day_first), _parse_date(co_raw, day_first)
        else:
            check_in, check_out = _parse_date(ci_raw, True), _parse_date(co_raw, True)
            if _is_ambiguous_date(ci_raw) or _is_ambiguous_date(co_raw):
                ambiguous += 1
                if nights:  # the stay length can settle which way round the dates are
                    for df in (True, False):
                        a, b = _parse_date(ci_raw, df), _parse_date(co_raw, df)
                        if a and b and (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days == int(nights):
                            check_in, check_out, ambiguous = a, b, ambiguous - 1
                            break
        if not check_in:
            bad_dates += 1
            continue
        if not check_out and nights:
            check_out = (datetime.date.fromisoformat(check_in) + datetime.timedelta(days=int(nights))).isoformat()
        if not check_out:
            bad_dates += 1
            continue
        if nights and (datetime.date.fromisoformat(check_out) - datetime.date.fromisoformat(check_in)).days != int(nights):
            night_mismatch += 1
        gross, fees, net = _num(raw(row, "gross")), _num(raw(row, "fees")), _num(raw(row, "net"))
        if net is None:
            net = _num(raw(row, "amount"))
        if net is None and gross is not None:
            net = gross - abs(fees or 0)
        if net is None:
            bad_dates += 1
            continue
        code = _clean_code(raw(row, "reservation_id"))
        if code:
            if code in seen:
                dup_codes.append(code)
                continue
            seen.add(code)
        listing, location = raw(row, "listing"), raw(row, "location")
        if status_raw:
            statuses[status_raw] += 1
        if raw(row, "currency"):
            currencies[str(raw(row, "currency")).strip().upper()] += 1
        items.append({"status": status_raw or None, "_exclude": bool(exclude_why), "_status_note": exclude_why,"reservation_id": code,
                      "description": str(listing).strip() or None if listing else None,
                      "location": " ".join(str(location).split()) if location else None,
                      "check_in": check_in, "check_out": check_out,
                      "gross": abs(gross) if gross is not None else abs(net),
                      "fees": abs(fees or 0), "net": abs(net), "confidence": 0.9 if code else 0.7,
                      "row": row_no})

    for kind, n in left_out.items():
        notes.append({"code": "rows_left_out", "level": "warn",
                      "message": f"{n} “{kind}” row{'s' if n != 1 else ''} {'were' if n != 1 else 'was'} left out — they aren't new reservations"
                                 + (f" but together change what you were paid by {_money(left_out_amt[kind])}" if left_out_amt[kind] else "")
                                 + ". If they relate to reservations you are importing, adjust those lines by hand."})
    if cancelled or odd_status:
        bits = []
        if cancelled:
            bits.append(f"{cancelled} cancelled or no-show")
        if odd_status:
            bits.append(f"{sum(odd_status.values())} with an unrecognised status ({', '.join(odd_status)})")
        notes.append({"code": "status_unticked", "level": "warn",
                      "message": f"{' and '.join(bits)} reservation{'s' if cancelled + sum(odd_status.values()) != 1 else ''} are listed with their source status but start unticked. Tick one only if it should count as income."})
    if currencies and set(currencies) != {"GBP"}:
        notes.append({"code": "currency", "level": "warn",
                      "message": f"This file has non-GBP amounts ({', '.join(f'{c} \u00d7{n}' for c, n in currencies.items())}). Amounts are imported as pounds without conversion."})
    shown = ", ".join(f"{h.strip() or '?'} \u2192 {label}" for label, i in (
        ("check-in", cols.get("check_in")), ("check-out", cols.get("check_out")), ("reservation ID", cols.get("reservation_id")),
        ("listing", cols.get("listing")), ("address", cols.get("location")), ("Gross Booking Revenue", cols.get("gross")),
        ("platform fee", cols.get("fees")), ("net", cols.get("net") if cols.get("net") is not None else cols.get("amount")),
        ("status", cols.get("status"))) if i is not None for h in [str(header[i])])
    gross_name = _h(header[cols["gross"]]) if "gross" in cols else ""
    meaning = ""
    if gross_name == "total payment" and "fees" in cols:
        meaning = (" Booking.com's \u201cTotal payment\u201d is taken as the full guest booking value (Gross Booking Revenue) and \u201cCommission\u201d as Booking.com's fee, "
                   "so you receive Total payment \u2212 Commission. Payment-service charges are not in this export; compare the first payout with your Booking.com statement.")
    notes.append({"code": "column_mapping", "level": "info", "message": f"Columns used: {shown}.{meaning}"})
    if dup_codes:
        notes.append({"code": "duplicate_codes", "level": "warn",
                      "message": f"{len(dup_codes)} reservation code{'s' if len(dup_codes) != 1 else ''} appeared more than once in the file ({', '.join(dup_codes[:4])}{'…' if len(dup_codes) > 4 else ''}); only the first was kept."})
    if ambiguous:
        notes.append({"code": "dates_ambiguous", "level": "warn",
                      "message": f"{ambiguous} date{'s' if ambiguous != 1 else ''} could be day/month or month/day and nothing in the file settles it; read as day/month (UK). Check the dates."})
    if night_mismatch:
        notes.append({"code": "nights_mismatch", "level": "warn",
                      "message": f"{night_mismatch} reservation{'s' if night_mismatch != 1 else ''} where check-out minus check-in doesn't equal the file's own Nights column. Check those dates."})
    if day_first is False:
        notes.append({"code": "dates_month_first", "level": "info", "message": "Dates in this file are month/day (US style) and were read that way."})
    return items, notes, tried - len(dup_codes)  # rows that should have become reservations; the caller reports any that didn't


def _load_key():
    """The Anthropic key from the environment or the project's .env file."""
    from services.env import get
    return get("ANTHROPIC_API_KEY")


def available():
    return bool(_load_key())


def extract_with_reason(file_path, mime_type=None, doc_type=None):
    """(result, failure): exactly one is None. `failure` is
    {"code", "message"} in plain words. Never raises — an upload that
    can't be read automatically must fall through to "enter it by hand",
    never a crash, no matter how malformed the file turns out to be."""
    try:
        result = _extract(file_path, mime_type, doc_type)
    except ExtractionFailure as e:
        return None, {"code": e.code, "message": e.message}
    except Exception as e:  # noqa: BLE001 — anything else is still "couldn't read it"
        log.warning("extraction crashed on %s: %s", Path(str(file_path)).name, type(e).__name__)
        return None, {"code": "unexpected", "message": "This file couldn't be read automatically. You can enter its rows manually."}
    if result is None:
        return None, {"code": "unreadable", "message": "Nothing could be read from this file. You can enter its rows manually."}
    return result, None


def extract(file_path, mime_type=None, doc_type=None):
    """doc_type == 'booking_statement' means "read reservations" (each
    item is a real booking with check-in/out); anything else reads plain
    money-in/money-out transaction lines. Returns the result or None."""
    return extract_with_reason(file_path, mime_type, doc_type)[0]


def _extract(file_path, mime_type, doc_type):
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
        raise ExtractionFailure("unreadable", "This file has no readable text (it may be binary or empty). You can enter its rows manually.")
    if name.endswith(".txt"):
        try:
            tabular = _extract_csv(file_path, reservations, doc_type)
        except ExtractionFailure:
            tabular = None
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
        raise ExtractionFailure("no_key", NO_KEY_MESSAGE)
    try:
        import anthropic
    except ImportError:
        raise ExtractionFailure("not_installed", "Automatic extraction isn't available because its library isn't installed. You can enter the rows manually.")

    if text is not None:
        content_block = {"type": "text", "text": "DOCUMENT TEXT:\n" + text[:60000]}
    else:
        size = os.path.getsize(file_path)
        if mime_type and mime_type.startswith("image/") and size > IMAGE_API_LIMIT:
            raise ExtractionFailure("too_large_to_read", f"This photo is {size / 1024 / 1024:.1f} MB, over the 5 MB limit for automatic reading. Upload a smaller or compressed photo, or enter the rows manually.")
        data = base64.standard_b64encode(open(file_path, "rb").read()).decode()
        if mime_type == "application/pdf":
            content_block = {"type": "document", "source": {"type": "base64", "media_type": mime_type, "data": data}}
        elif mime_type and mime_type.startswith("image/"):
            content_block = {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": data}}
        else:
            raise ExtractionFailure("unsupported", "This kind of file can't be read automatically. You can enter its rows manually.")

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=4096 if reservations else 2048,
            messages=[{"role": "user", "content": [content_block, {"type": "text", "text": prompt}]}],
        )
    except Exception as e:  # noqa: BLE001 -- network/auth/quota: report the class only, never the key or payload
        log.warning("extraction API call failed: %s", type(e).__name__)
        raise ExtractionFailure("api_error", f"Automatic extraction didn't complete ({type(e).__name__}). Try again later, or enter the rows manually.")

    out = "".join(block.text for block in response.content if block.type == "text").strip()
    if out.startswith("```"):
        out = out.strip("`")
        out = out.split("\n", 1)[1] if "\n" in out else out
    try:
        result = json.loads(out)
    except ValueError:
        raise ExtractionFailure("bad_response", "The document was sent for reading but the reply couldn't be understood. Try again, or enter the rows manually.")
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise ExtractionFailure("bad_response", "The document was sent for reading but the reply couldn't be understood. Try again, or enter the rows manually.")
    return {
        "items": result["items"],
        "document_hint": result.get("document_hint"),
        "period_hint": result.get("period_hint"),
        "kind": "reservation" if reservations else "transaction",
        "platform": result.get("platform"),
    }


_COLUMN_HINTS = {
    "date": ("date",),
    "vendor": ("vendor", "payee", "merchant", "description 1", "name"),
    "description": ("description", "memo", "details", "narrative"),
    "amount": ("amount", "net total", "total", "value", "debit", "credit"),
}
_NOT_AN_AMOUNT = ("subtotal", "vat", "tax", "fee", "quantity", "qty", "count", "balance")


def _guess_columns(header):
    lower = [_h(h) for h in header]
    found = {}
    for field, hints in _COLUMN_HINTS.items():
        for hint in hints:  # earlier hints are the better evidence
            idx = next((i for i, col in enumerate(lower)
                        if _h(hint) in col and i not in found.values()
                        and not (field == "amount" and any(bad in col for bad in _NOT_AN_AMOUNT))), None)
            if idx is not None:
                found[field] = idx
                break
    return found


_DOC_TYPE_CATEGORY = {"amazon_order": "purchase", "cleaning_invoice": "cleaning", "utility_bill": "utilities"}


def _rows_to_items(header, rows, doc_type=None):
    cols = _guess_columns(header)
    if "amount" not in cols:
        return None  # can't make sense of this sheet without an amount column
    day_first = _detect_day_first([row[cols["date"]] for row in rows if "date" in cols and cols["date"] < len(row)])
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
            "date": _parse_date(row[cols["date"]], day_first is not False) if "date" in cols and cols["date"] < len(row) else None,
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
    try:
        with open(file_path, newline="", encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(4096)
            f.seek(0)
            delimiter = "\t" if str(file_path).lower().endswith(".tsv") else _sniff_delimiter(sample)
            rows = list(csv.reader(f, delimiter=delimiter))
    except (csv.Error, OSError):
        raise ExtractionFailure("malformed_spreadsheet", "This spreadsheet couldn't be opened — it looks corrupted or isn't really a CSV. Re-export it, or enter the rows manually.")
    return _sheet_result(rows, reservations, doc_type)


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
        raise ExtractionFailure("not_installed", "Old-format .xls files can't be read here. Save it as .xlsx or .csv and upload that, or enter the rows manually.")
    try:
        book = xlrd.open_workbook(file_path)
        ws = book.sheet_by_index(0)
    except Exception:
        raise ExtractionFailure("malformed_spreadsheet", "This spreadsheet couldn't be opened — it looks corrupted. Re-export it, or enter the rows manually.")

    def value(r, c):
        v = ws.cell_value(r, c)
        if ws.cell_type(r, c) == xlrd.XL_CELL_DATE:  # a real Excel date, not text
            try:
                return xlrd.xldate_as_datetime(v, book.datemode).strftime("%Y-%m-%d")
            except Exception:  # noqa: BLE001
                return v
        return v

    rows = [[value(r, c) for c in range(ws.ncols)] for r in range(ws.nrows)]
    return _sheet_result(rows, reservations, doc_type)


def _extract_xlsx(file_path, reservations=False, doc_type=None):
    try:
        import openpyxl
    except ImportError:
        raise ExtractionFailure("not_installed", "Excel files can't be read here. Save it as .csv and upload that, or enter the rows manually.")
    try:
        ws = openpyxl.load_workbook(file_path, data_only=True).active
        rows = [[c.value for c in row] for row in ws.iter_rows()]
    except Exception:
        raise ExtractionFailure("malformed_spreadsheet", "This spreadsheet couldn't be opened — it looks corrupted or password-protected. Re-export it, or enter the rows manually.")
    return _sheet_result(rows, reservations, doc_type)


def _is_amazon_business(header):
    lower = {_h(h) for h in header}
    return {"order id", "asin"} <= lower and bool({"item net total", "item subtotal"} & lower)


def _rows_to_amazon_items(header, rows):
    """Amazon Business order history: one row per ITEM, with the whole
    order's totals repeated on every row of that order (so reading "Order
    Subtotal" would count a ten-item order ten times). Each item's own
    Item Net Total (what was paid, VAT included) becomes one line.
    -> (items, notes, rows_tried)."""
    from collections import Counter
    idx = {_h(h): i for i, h in enumerate(header)}

    def cell(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    day_first = _detect_day_first([cell(r, "order date") for r in rows])
    items, notes = [], []
    cancelled = refunds = tried = 0
    orders = set()
    for row_no, row in enumerate(rows, start=2):
        if not any(str(c or "").strip() for c in row):
            continue
        status = str(cell(row, "order status") or "").lower()
        if "cancel" in status:
            cancelled += 1
            continue
        tried += 1
        net = _num(cell(row, "item net total"))
        if net is None:
            sub = _num(cell(row, "item subtotal"))
            net = None if sub is None else sub + (_num(cell(row, "item vat")) or 0)
        if net is None:
            continue
        if net <= 0:
            refunds += 1
            continue
        qty = int(_num(cell(row, "item quantity")) or 1)
        title = " ".join(str(cell(row, "title") or "").split())
        order_id = str(cell(row, "order id") or "").strip()
        orders.add(order_id)
        items.append({"date": _parse_date(cell(row, "order date"), day_first is not False), "vendor": "Amazon",
                      "description": (title[:110] + ("…" if len(title) > 110 else "")) + (f" ×{qty}" if qty > 1 else ""),
                      "amount": round(net, 2), "category": "purchase", "confidence": 0.95, "row": row_no,
                      "order_id": order_id, "po": str(cell(row, "po number") or "").strip(),
                      "seller": str(cell(row, "seller name") or "").strip() or None})
    notes.append({"code": "amazon_amounts", "level": "info",
                  "message": f"Amazon Business order history: {len(items)} item line{'s' if len(items) != 1 else ''} from {len(orders)} order{'s' if len(orders) != 1 else ''}. Each amount is the item's own \u201cItem Net Total\u201d (what was paid, including VAT), not the repeated order total."})
    if cancelled:
        notes.append({"code": "cancelled_left_out", "level": "info", "message": f"{cancelled} item row{'s' if cancelled != 1 else ''} from cancelled orders left out."})
    if refunds:
        notes.append({"code": "refunds_left_out", "level": "warn", "message": f"{refunds} item row{'s' if refunds != 1 else ''} with a zero or negative total (returns?) were left out. Check them against your records."})
    return items, notes, tried


_PO_BUSINESS = {"biz", "business", "company", "office", "overhead", "overheads", "admin"}
_PO_PERSONAL = {"personal", "private", "own", "me"}
_PO_ALL = {"all flats", "all", "all properties", "every flat", "shared", "general"}


def _alnum(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def resolve_po(po_text, properties, overhead_id=None):
    """Read an Amazon PO Number as the property it was bought for.
    -> (property_id | None, action, note), where action is one of
    assign | business | exclude | shared | ambiguous | unknown | none.
    A code is matched against each property's own code ("LW", "W8", "11PW"),
    never guessed, and several properties in one PO is ambiguous, not split."""
    text = (po_text or "").strip()
    if not text:
        return None, "none", "no PO number"
    low = " ".join(text.lower().split())
    if low in _PO_PERSONAL:
        return None, "exclude", f"PO says \u201c{text}\u201d"
    if low in _PO_BUSINESS:
        return overhead_id, ("business" if overhead_id else "unknown"), f"PO says \u201c{text}\u201d (business cost)"
    if low in _PO_ALL:
        return None, "shared", f"PO says \u201c{text}\u201d — bought for several properties"
    by_code = {}
    for p in properties:
        for key in (p.get("code"), p["id"], p["name"]):
            if _alnum(key):
                by_code.setdefault(_alnum(key), p)
    tokens = [t for t in re.split(r"\s*(?:,|;|/|&|\band\b|\+)\s*", text, flags=re.I) if t.strip()]
    matched, unmatched = {}, []
    for t in tokens:
        hit = by_code.get(_alnum(t))
        if hit:
            matched[hit["id"]] = hit
        else:
            unmatched.append(t.strip())
    if len(matched) == 1 and not unmatched:
        only = next(iter(matched.values()))
        return only["id"], "assign", f"PO \u201c{text}\u201d = {only['name']}"
    if matched:
        names = ", ".join(p["name"] for p in matched.values())
        return None, "ambiguous", f"PO \u201c{text}\u201d names several properties ({names}" + (f"; and \u201c{', '.join(unmatched)}\u201d isn't one" if unmatched else "") + ")"
    return None, "unknown", f"PO \u201c{text}\u201d doesn't match a property in the dashboard"


def _sheet_result(rows, reservations, doc_type=None):
    """One reader for csv/xls/xlsx. Raises a specific ExtractionFailure for
    an empty sheet or unrecognised columns. `rows_seen` is how many rows
    should have become lines, so the caller can report any that didn't, and
    `notes` is everything the reader deliberately left out or wasn't sure of."""
    if not rows or not any(str(c or "").strip() for c in rows[0]):
        raise ExtractionFailure("empty_file", "This file is empty (no header row). You can enter its rows manually.")
    header = [str(c) if c is not None else "" for c in rows[0]]
    data_rows = rows[1:]
    rows_seen = sum(1 for r in data_rows if any(str(c or "").strip() for c in r))
    if reservations:
        items, notes, tried = _rows_to_reservations(header, data_rows)
        if items is None:
            raise ExtractionFailure("columns_not_recognised", "This doesn't look like a booking statement: no check-in date and payout/amount columns were found. Check the document type, or enter the reservations manually.")
        return {"items": items, "document_hint": None, "period_hint": None, "kind": "reservation", "platform": _platform_of(header),
                "rows_seen": tried, "notes": notes}
    if _is_amazon_business(header):
        items, notes, tried = _rows_to_amazon_items(header, data_rows)
        return {"items": items, "document_hint": None, "period_hint": None, "kind": "transaction", "rows_seen": tried, "notes": notes, "source_kind": "amazon_business"}
    items = _rows_to_items(header, data_rows, doc_type)
    if items is None:
        raise ExtractionFailure("columns_not_recognised", "No Amount column was found. Expected headers like Date, Vendor, Description and Amount. Rename the columns, or enter the rows manually.")
    return {"items": items, "document_hint": None, "period_hint": None, "kind": "transaction", "rows_seen": rows_seen, "notes": []}


_NON_WORD = re.compile(r"[^a-z0-9]+")


# Words that appear in many addresses and so identify none of them.
_GENERIC_WORDS = {"flat", "apartment", "apartments", "apt", "unit", "house", "court", "road", "rd", "street", "st", "way", "ave", "avenue",
                  "building", "buildings", "wharf", "lane", "park", "place", "square", "hill", "gardens", "terrace", "close", "drive",
                  "view", "lodge", "tower", "towers", "mansions", "mews", "the", "and", "london", "greater"}


def _words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _is_strong(word):
    """A word that can identify a property on its own: starts with a letter,
    isn't a throwaway like Hill/Way/Court, and isn't just a house number."""
    return word[0].isalpha() and word not in _GENERIC_WORDS and (len(word) >= 3 or any(ch.isdigit() for ch in word))


def _score_text(candidate, hint):
    """(share of the property's words found, number found, strong words found)."""
    words = [w for w in _words(candidate) if len(w) >= 2 and w not in ("the", "and")]
    if not words:
        return 0.0, 0, 0
    hit_words = [w for w in words if w in hint]
    return len(hit_words) / len(words), len(hit_words), sum(1 for w in hit_words if _is_strong(w))


def rank_properties(hint_text, properties):
    """Every property whose name/address the text really names, best first:
    [{"id", "name", "score", "hits"}]. Whole words only (so "Hideaway" is not
    "way"), and at least one distinguishing word must match — "Hill" alone
    never identifies a property. A wrong silent match is worse than none: an
    unmatched line is simply asked about once and remembered. The name is
    trusted before the address."""
    hint = set(_words(hint_text))
    if not hint:
        return []
    out = []
    for p in properties:
        score = None
        for candidate in (p["name"], p["address"] or ""):
            share, hits, strong = _score_text(candidate, hint)
            if strong:
                score = (share, hits)
                break
        if score and score[0] >= 0.5:
            out.append({"id": p["id"], "name": p["name"], "score": round(score[0], 2), "hits": score[1]})
    # a longer name that matches in full is more specific than a shorter one inside it
    return sorted(out, key=lambda c: (-c["score"], -c["hits"]))


def confident_property(hint_text, properties):
    """The property id the text names, or None when it names none -- or names
    two about equally well (e.g. "Miles Building" with no number)."""
    ranked = rank_properties(hint_text, properties)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[1]["score"] >= ranked[0]["score"] - 0.1 and ranked[1]["hits"] >= ranked[0]["hits"]:
        return None
    return ranked[0]["id"]


def guess_property(hint_text, properties):
    """Fuzzy-matches free text (from document_hint, or a filename) against
    known property names/addresses. Returns (property_id, confidence) or
    (None, 0) — confidence is just "how much of the property's own name
    matched", not a calibrated probability."""
    ranked = rank_properties(hint_text, properties)
    return (ranked[0]["id"], ranked[0]["score"]) if ranked else (None, 0.0)
