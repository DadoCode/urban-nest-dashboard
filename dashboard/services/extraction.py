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
"utilities", "other" (anything the business paid out), \
"confidence": number from 0 to 1, how sure you are this line is read correctly}
  ]
}

Only extract what's actually printed on the document -- never invent a vendor, \
amount or date that isn't there. If the document has one single total rather \
than line items, return that as a single item. On a bank statement, skip \
anything that's clearly a personal transaction rather than this business's."""


def _load_key():
    return os.environ.get("ANTHROPIC_API_KEY")


def available():
    return bool(_load_key())


def extract(file_path, mime_type=None):
    mime_type = mime_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    name = str(file_path).lower()
    if name.endswith(".csv") or mime_type == "text/csv":
        return _extract_csv(file_path)
    if name.endswith(".xlsx") or mime_type in ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",):
        return _extract_xlsx(file_path)
    return _extract_via_claude(file_path, mime_type)


def _extract_via_claude(file_path, mime_type):
    api_key = _load_key()
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

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
            max_tokens=2048,
            messages=[{"role": "user", "content": [content_block, {"type": "text", "text": EXTRACTION_PROMPT}]}],
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


def _rows_to_items(header, rows):
    cols = _guess_columns(header)
    if "amount" not in cols:
        return None  # can't make sense of this sheet without an amount column
    items = []
    for row in rows:
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
            "date": row[cols["date"]] if "date" in cols and cols["date"] < len(row) else None,
            "vendor": row[cols["vendor"]] if "vendor" in cols and cols["vendor"] < len(row) else None,
            "description": row[cols["description"]] if "description" in cols and cols["description"] < len(row) else None,
            "amount": abs(amount),
            "category": "booking_income" if amount > 0 else "other",
            "confidence": 0.6,  # heuristic column-guessing, not a verified read
        })
    return items


def _extract_csv(file_path):
    with open(file_path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return None
    items = _rows_to_items(rows[0], rows[1:])
    if items is None:
        return None
    return {"items": items, "document_hint": None, "period_hint": None}


def _extract_xlsx(file_path):
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
    items = _rows_to_items(header, rows[1:])
    if items is None:
        return None
    return {"items": items, "document_hint": None, "period_hint": None}


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
