"""Optional login. Off by default (local use needs none). Turn it on by
setting UN_OWNER_PASSWORD (full access) and, to share the dashboard, a
UN_VIEWER_PASSWORD (read-only) in the environment or the project's .env.
Once on, every page needs a login, and the viewer role can look at
everything but change nothing -- enforced here on the server, not just by
hiding buttons."""
import hmac
import time
from urllib.parse import urlparse

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for

import services.env as env

bp = Blueprint("auth", __name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_FAILS, LOCK_SECONDS = 5, 300
_fails = {}   # client -> (count, first_failure_ts)


def passwords():
    return {"owner": env.get("UN_OWNER_PASSWORD"), "viewer": env.get("UN_VIEWER_PASSWORD")}


def enabled():
    return bool(passwords()["owner"])


def _client():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()


def _locked(client):
    count, first = _fails.get(client, (0, 0))
    if count >= MAX_FAILS and time.time() - first < LOCK_SECONDS:
        return int(LOCK_SECONDS - (time.time() - first))
    if count and time.time() - first >= LOCK_SECONDS:
        _fails.pop(client, None)
    return 0


def _safe_next(target):
    p = urlparse(target or "")
    return target if target and not p.netloc and not p.scheme and target.startswith("/") and not target.startswith("//") else url_for("overview.index")


@bp.before_app_request
def require_login():
    if not enabled():
        g.role = "owner"
        return None
    if request.endpoint in ("auth.login", "static"):
        return None
    role = session.get("role")
    if role not in ("owner", "viewer"):
        if request.headers.get("HX-Request"):
            return jsonify(error="login required"), 401
        return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
    g.role = role
    if role == "viewer" and request.method not in SAFE_METHODS and request.endpoint != "auth.logout":
        if request.headers.get("HX-Request"):
            return "<p class='note'>This is a view-only account.</p>", 403
        flash("This is a view-only account, so it can't change anything. Ask the owner if something needs updating.", "warning")
        return redirect(request.referrer or url_for("overview.index"))
    return None


@bp.after_app_request
def no_index(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not enabled():
        return redirect(url_for("overview.index"))
    error = None
    if request.method == "POST":
        client = _client()
        wait = _locked(client)
        if wait:
            error = f"Too many attempts. Try again in {wait // 60 + 1} minute{'s' if wait > 60 else ''}."
        else:
            given = (request.form.get("password") or "").encode()
            role = None
            for name, pw in passwords().items():
                if pw and hmac.compare_digest(given, pw.encode()):
                    role = name
                    break
            if role:
                _fails.pop(client, None)
                session.clear()
                session["role"] = role
                session.permanent = True
                return redirect(_safe_next(request.form.get("next")))
            count, first = _fails.get(client, (0, time.time()))
            _fails[client] = (count + 1, first)
            error = "That password isn't right. Check it and try again."
    return render_template("login.html", error=error, next=request.args.get("next") or request.form.get("next") or "")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login") if enabled() else url_for("overview.index"))
