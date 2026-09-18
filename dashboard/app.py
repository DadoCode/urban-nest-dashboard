"""
Urban Nest Estates business dashboard.

Every KPI on every page is derived on read from `bookings` + `transactions`
via services/kpis.py -- there is no cached totals table to keep in sync.
See scripts/migrate_to_normalized_schema.py for how the original Excel
import's monthly_summary/expense_items/goals became this shape.

Routes live in routes/ (one blueprint module per page area), reusable
non-Flask logic in services/ (kpis, insights, extraction, documents,
ical_sync) -- this file just builds the app and wires them together.

Run with:  python3 dashboard/app.py
"""
import os
from pathlib import Path

from flask import Flask, request

import db
from routes import register_blueprints
from services.completeness import seed_defaults

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = Path(os.environ["DASHBOARD_UPLOADS_PATH"]) if os.environ.get("DASHBOARD_UPLOADS_PATH") else ROOT / "data" / "uploads"

RECENT_COOKIE = "recent_properties"
RECENT_MAX = 5


def create_app():
    flask_app = Flask(__name__)
    flask_app.secret_key = "urban-nest-dashboard"  # local-only tool, no auth/session sensitivity

    db.ensure_schema()
    conn = db.get_conn()
    for p in conn.execute("SELECT id FROM properties WHERE type='flat'"):
        seed_defaults(conn, p["id"])
    conn.commit()
    conn.close()

    @flask_app.before_request
    def _setup():
        db.ensure_schema()
        UPLOADS.mkdir(parents=True, exist_ok=True)

    @flask_app.context_processor
    def _inject_recently_viewed():
        # Replaces the old permanently-listed-every-property sidebar: the
        # last few properties actually visited, read from a plain cookie
        # (no server-side session store needed for a solo-operator tool).
        raw = request.cookies.get(RECENT_COOKIE, "")
        ids = [i for i in raw.split(",") if i][:RECENT_MAX]
        if not ids:
            return {"recently_viewed": []}
        conn = db.get_conn()
        by_id = {p["id"]: p for p in conn.execute(
            f"SELECT id, name FROM properties WHERE id IN ({','.join('?' * len(ids))})", ids
        )}
        return {"recently_viewed": [by_id[i] for i in ids if i in by_id]}

    register_blueprints(flask_app)
    return flask_app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, port=5050)
