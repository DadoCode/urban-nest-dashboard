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

from flask import Flask, flash, redirect, request, url_for

import db
from routes import register_blueprints
from services import runtime
from services.completeness import seed_defaults

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = runtime.uploads_dir()

RECENT_COOKIE = "recent_properties"
RECENT_MAX = 5


def _secret_key():
    """A random key, created once and kept next to the database so logins
    survive restarts. Falls back to a per-run key if the folder is read-only."""
    import secrets
    from services.env import get
    if get("UN_SECRET_KEY"):
        return get("UN_SECRET_KEY")
    if runtime.is_demo():
        # The hosted demo has no login and no durable disk; a per-instance random key
        # would make a flash message set on one serverless instance vanish on the next.
        return "urban-nest-read-only-demo"
    path = ROOT / "data" / ".secret_key"
    try:
        if path.exists():
            return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        path.write_text(key)
        path.chmod(0o600)
        return key
    except OSError:
        return secrets.token_hex(32)


def _token_gate(app, token):
    """Serve the app only under /s/<token>/ ; every other path is a bare 404
    that reveals nothing. The token becomes Flask's SCRIPT_NAME, so every
    url_for() link and asset URL the pages produce keeps it automatically
    (no cookies needed, so a browsing tool can follow links)."""
    import hmac
    prefix = f"/s/{token}"

    def gated(environ, start_response):
        path = environ.get("PATH_INFO", "")
        candidate = path[:len(prefix)]
        if hmac.compare_digest(candidate.encode(), prefix.encode()) and path[len(prefix):len(prefix) + 1] in ("", "/"):
            environ["SCRIPT_NAME"] = prefix
            environ["PATH_INFO"] = path[len(prefix):] or "/"
            return app(environ, start_response)
        start_response("404 Not Found", [("Content-Type", "text/plain"), ("X-Robots-Tag", "noindex")])
        return [b"Not found"]

    return gated


def create_app():
    flask_app = Flask(__name__)
    flask_app.secret_key = _secret_key()
    flask_app.config["MAX_CONTENT_LENGTH"] = runtime.max_upload_bytes()
    demo = runtime.is_demo()
    flask_app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                            PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)
    # behind a tunnel (ngrok/Cloudflare) the real scheme/client arrive in X-Forwarded-* headers
    from werkzeug.middleware.proxy_fix import ProxyFix
    flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    token = runtime.share_token()
    if token:
        flask_app.wsgi_app = _token_gate(flask_app.wsgi_app, token)

        @flask_app.after_request
        def _private_headers(response):
            # No search indexing, and never send this URL (it carries the token) to the CDNs the pages load scripts from.
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "private, no-store"
            return response
    @flask_app.before_request
    def _secure_cookie_over_https():
        flask_app.config["SESSION_COOKIE_SECURE"] = request.is_secure

    # The demo database ships already migrated and its filesystem is
    # read-only, so it must never try to alter or seed anything.
    if not demo:
        db.ensure_schema()
        conn = db.get_conn()
        for p in conn.execute("SELECT id FROM properties WHERE type='flat'"):
            seed_defaults(conn, p["id"])
        conn.commit()
        conn.close()

    @flask_app.context_processor
    def _inject_runtime():
        return {"demo_mode": demo, "demo_message": runtime.refusal_message(), "demo_banner": runtime.banner_text(),
                "max_upload_mb": round(runtime.max_upload_bytes() / 1024 / 1024)}

    def _back(default_endpoint):
        ref = request.referrer or ""
        return ref if ref.startswith(request.host_url) else url_for(default_endpoint)

    @flask_app.before_request
    def _demo_read_only():
        """Refuse every write on the hosted preview before it can touch the
        (read-only, non-durable) filesystem -- a calm explanation, never a
        500, and never a half-saved record that would later vanish."""
        if not demo or request.method in ("GET", "HEAD", "OPTIONS") or request.endpoint == "auth.login":
            return None
        if request.headers.get("HX-Request"):
            return f'<div class="note">{runtime.refusal_message()}</div>', 200
        flash(runtime.refusal_message(), "warning")
        return redirect(_back("overview.index"))

    @flask_app.errorhandler(413)
    def _too_large(_err):
        limit = round(runtime.max_upload_bytes() / 1024 / 1024)
        msg = f"That upload is larger than the {limit} MB limit, so nothing was saved. Upload a smaller file, or split a long PDF into parts."
        if request.headers.get("HX-Request"):
            return f'<div class="note">{msg}</div>', 413
        flash(msg, "error")
        return redirect(_back("documents.index"))

    @flask_app.context_processor
    def _inject_provenance():
        """Which workbook import last updated the figures on this page (shown quietly under the context bar)."""
        from flask import g
        import db as _db
        from services.workbook import batches as _wb

        def workbook_provenance(ctx):
            try:
                lo = f"{ctx['start_year']}-{ctx['start_month']:02d}"
                hi = f"{ctx['end_year']}-{ctx['end_month']:02d}"
                return _wb.provenance_for_range(_db.get_conn(), ctx.get("property_id"), lo, hi)
            except Exception:
                return None
        return {"workbook_provenance": workbook_provenance}

    @flask_app.context_processor
    def _inject_auth():
        from flask import g
        from routes.auth import enabled
        return {"auth_on": enabled(), "role": g.get("role", "owner")}

    @flask_app.context_processor
    def _asset_versions():
        # file mtime in the URL so a browser never serves stale CSS/JS after an update
        static = Path(__file__).resolve().parent / "static"
        return {"asset_v": max((f.stat().st_mtime_ns for f in static.iterdir() if f.is_file()), default=0)}

    @flask_app.after_request
    def _remember_context(response):
        from flask import g
        saved = g.get("ctx_save")
        if saved:
            response.set_cookie("un_ctx", saved, max_age=60 * 60 * 24 * 90, samesite="Lax")
        return response

    @flask_app.context_processor
    def _inject_nav_context():
        from flask import g
        qs = g.get("ctx_save") or request.cookies.get("un_ctx", "")
        return {"nav_qs": ("?" + qs) if qs else ""}

    @flask_app.before_request
    def _setup():
        if demo:
            return
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

    from services.completeness import DOC_TYPE_LABELS

    def money(v, places=0):
        if v is None:
            return "—"
        sign = "−" if v < 0 else ""
        return f"{sign}£{abs(v):,.{places}f}"

    def money_k(v):
        if v is None:
            return "—"
        sign = "−" if v < 0 else ""
        a = abs(v)
        return f"{sign}£{a / 1000:.1f}k" if a >= 1000 else f"{sign}£{a:.0f}"

    flask_app.jinja_env.filters["money"] = money
    flask_app.jinja_env.filters["money2"] = lambda v: money(v, 2)
    flask_app.jinja_env.filters["money_k"] = money_k
    flask_app.jinja_env.filters["pct"] = lambda v, places=0: "—" if v is None else f"{v:.{places}f}%"
    from services.common import CHANNELS, METRIC_INFO, channel_key
    flask_app.jinja_env.filters["channel_key"] = channel_key
    flask_app.jinja_env.globals["CHANNELS"] = CHANNELS
    flask_app.jinja_env.globals["METRIC_INFO"] = METRIC_INFO
    flask_app.jinja_env.filters["doc_label"] = lambda k: DOC_TYPE_LABELS.get(k, k)

    def xurl(base, endpoint="expenses.index", **extra):
        """url_for(endpoint) carrying the shared period/property/compare params, plus extras."""
        from flask import url_for
        return url_for(endpoint, **{**base, **extra})

    def cx(endpoint, keep_property=False, **values):
        """url_for(endpoint) that carries the user's current period/compare
        (and the property too when keep_property) so drilling into another
        page never lands on a different month than the one they chose."""
        from flask import url_for
        from services.context import link_params
        return url_for(endpoint, **link_params(keep_property, **values))

    def wsurl(ctx, endpoint, property_id, **extra):
        """A link into one property's workspace tab carrying the selected period and comparison explicitly."""
        from flask import url_for
        from services.context import workspace_params
        return url_for(endpoint, property_id=property_id, **workspace_params(ctx), **extra)

    flask_app.jinja_env.globals["xurl"] = xurl
    flask_app.jinja_env.globals["cx"] = cx
    flask_app.jinja_env.globals["wsurl"] = wsurl

    register_blueprints(flask_app)
    return flask_app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, port=5050)
