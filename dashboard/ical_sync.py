"""
Pulls occupancy straight from a listing's own booking calendar -- the iCal
export URL every major platform provides (Airbnb: listing > Availability >
Export calendar; Booking.com/Vrbo: Sync calendars). No API keys, no
scraping: it's a plain .ics text file listing each reservation as one
all-day VEVENT block.

Kept dependency-free (stdlib only) since the .ics format used by these
exports is simple and consistent enough not to need a parsing library.
"""
import datetime
import re
import urllib.error
import urllib.request
from collections import defaultdict

_EVENT_RE = re.compile(r"BEGIN:VEVENT(.*?)END:VEVENT", re.DOTALL)
_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")


def fetch(url, timeout=15):
    """Returns the raw .ics text, or raises ValueError with a message fit
    to show the user directly."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; UrbanNestDashboard/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise ValueError(f"The calendar URL returned an error ({e.code}) -- double check it's the right export link.")
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach that calendar URL: {e.reason}")
    except TimeoutError:
        raise ValueError("Timed out reaching that calendar URL.")


def _parse_ics_date(value):
    """DTSTART/DTEND values are either 'YYYYMMDD' (all-day) or
    'YYYYMMDDTHHMMSSZ' (timed) -- only the date part matters here."""
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        return datetime.date(y, mo, d)
    except ValueError:
        return None


def parse_events(ics_text):
    """[(start_date, end_date), ...] for every VEVENT -- end_date is the
    iCal convention's exclusive checkout day (the night before it is the
    last night booked)."""
    events = []
    for block in _EVENT_RE.findall(ics_text):
        start = end = None
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("DTSTART"):
                start = _parse_ics_date(line.split(":", 1)[-1])
            elif line.startswith("DTEND"):
                end = _parse_ics_date(line.split(":", 1)[-1])
        if start and end and end > start:
            events.append((start, end))
    return events


def nights_by_month(events):
    """{(year, month): nights_booked} by walking every night in every
    event -- an event spanning a month boundary splits across both."""
    counts = defaultdict(int)
    for start, end in events:
        night = start
        while night < end:
            counts[(night.year, night.month)] += 1
            night += datetime.timedelta(days=1)
    return counts


def sync(url):
    """Fetches + parses a calendar URL end to end. Returns {(year, month):
    nights_booked}. Raises ValueError (safe to flash to the user) on any
    fetch/parse problem."""
    ics_text = fetch(url)
    events = parse_events(ics_text)
    if not events:
        raise ValueError("That calendar loaded fine but had no bookings in it -- double check it's the export/sync URL, not the listing page itself.")
    return nights_by_month(events)
