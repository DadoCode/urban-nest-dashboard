"""
Extracts structured expense line items (vendor, description, amount,
category, date) from an uploaded document -- an Amazon order confirmation,
a cleaner's invoice, an Airbnb/booking payout statement, a receipt photo --
using Claude when an API key is available.

Mirrors this repo's existing "works with zero setup" pattern (see
generator.py / planner.py): with no ANTHROPIC_API_KEY, extract() returns
None and the app falls back to a manual entry form instead of crashing.
"""
import base64
import json
import mimetypes
import os

EXTRACTION_PROMPT = """You are looking at a document from a UK short-term-rental \
business: it could be an Amazon/Temu order confirmation, a cleaner's invoice, \
a utility bill, an Airbnb/booking-platform payout statement, or a bank statement \
(which can mix several of the above in one file, one transaction per line).

Extract every distinct transaction line you can find. Return ONLY a JSON array \
(no prose, no markdown fences), each item shaped like:
{"date": "YYYY-MM-DD or null if unclear", "vendor": "string", \
"description": "short string", "amount": number, ALWAYS POSITIVE -- a plain \
magnitude, never negative, "category": one of "booking_income" (money the \
business received from a guest/booking platform), "purchase", "cleaning", \
"utilities", "other" (anything the business paid out)}

Only extract what's actually printed on the document -- never invent a vendor, \
amount or date that isn't there. If the document has one single total rather \
than line items, return that as a single item. On a bank statement, skip \
anything that's clearly a personal transaction rather than this business's."""


def _load_key():
    return os.environ.get("ANTHROPIC_API_KEY")


def available():
    return bool(_load_key())


def extract(file_path, mime_type=None):
    """Returns a list of dicts (see EXTRACTION_PROMPT shape) or None if no
    API key is configured / extraction failed -- callers should fall back
    to manual entry in either case."""
    api_key = _load_key()
    if not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        return None

    mime_type = mime_type or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    data = base64.standard_b64encode(open(file_path, "rb").read()).decode()

    if mime_type == "application/pdf":
        content_block = {"type": "document", "source": {"type": "base64", "media_type": mime_type, "data": data}}
    elif mime_type.startswith("image/"):
        content_block = {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": data}}
    else:
        return None  # unsupported file type for extraction

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=2048,
            messages=[{"role": "user", "content": [content_block, {"type": "text", "text": EXTRACTION_PROMPT}]}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[1] if "\n" in text else text
        items = json.loads(text)
        return items if isinstance(items, list) else None
    except Exception:
        return None
