"""Where this process is running, and what it is allowed to persist.

Two environments exist on purpose:
  * local app -- the authoritative one: a writable data/ folder, so uploads
    and confirmed records are durable.
  * hosted Vercel preview -- a READ-ONLY demo: its filesystem is read-only
    and any /tmp scratch space vanishes between invocations, so nothing
    written there can be trusted. Every mutation is refused up front rather
    than half-working and then disappearing.
"""
import os
from pathlib import Path

from services import env

ROOT = Path(__file__).resolve().parent.parent.parent

DEMO_MESSAGE = "Demo mode — changes and uploads are disabled on this hosted preview."


def is_demo():
    """True on Vercel (it always sets VERCEL) or when UN_DEMO_MODE is set,
    so the read-only behaviour can also be exercised locally."""
    if os.environ.get("VERCEL"):
        return True
    return (env.get("UN_DEMO_MODE") or "").lower() in ("1", "true", "yes", "on")


def uploads_dir():
    """The configured upload folder: DASHBOARD_UPLOADS_PATH if set,
    otherwise data/uploads next to the database."""
    configured = env.get("DASHBOARD_UPLOADS_PATH")
    return Path(configured) if configured else ROOT / "data" / "uploads"


def max_upload_bytes():
    """Per-request upload ceiling (UN_MAX_UPLOAD_MB, default 20 MB). Kept
    below the extraction API's own request limit once a file is base64'd."""
    try:
        mb = float(env.get("UN_MAX_UPLOAD_MB") or 20)
    except ValueError:
        mb = 20
    return int(mb * 1024 * 1024)
