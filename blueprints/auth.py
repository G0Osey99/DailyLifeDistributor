"""Login/logout routes and the login_required decorator."""
from __future__ import annotations

import os
import urllib.parse
from functools import wraps

from flask import (
    Blueprint, redirect, render_template, request, session, url_for,
)

from core import auth

bp = Blueprint("auth", __name__)

_SESSION_KEY = "authenticated"

# When deployed behind the Cloudflare Tunnel, request.remote_addr is the
# Caddy container — so a remote_addr-keyed lockout would be a single global
# bucket: any attacker could lock the one shared account for everyone (DoS),
# and per-attacker isolation is lost. Cloudflare sets CF-Connecting-IP to the
# real client and strips any client-supplied copy, so it's safe to trust when
# we know we're behind it (HOSTED). Local/dev (no proxy) uses remote_addr.
_HOSTED = (os.environ.get("HOSTED", "") or "").lower() in ("1", "true", "yes")


def _client_ip() -> str:
    if _HOSTED:
        cf = (request.headers.get("CF-Connecting-IP") or "").strip()
        if cf:
            return cf
    return request.remote_addr or "unknown"


def _safe_next(nxt: str) -> str:
    """Return nxt only if it's a same-origin relative path, else the index.

    Rejects absolute URLs, protocol-relative `//host`, and backslash variants
    that browsers may normalize to `//host`.
    """
    nxt = (nxt or "").strip()
    if not nxt or nxt.startswith("//") or nxt.startswith("/\\") or "\\" in nxt:
        return url_for("scan.index")
    parsed = urllib.parse.urlparse(nxt)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return url_for("scan.index")
    return nxt


def is_authenticated() -> bool:
    return bool(session.get(_SESSION_KEY))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/login", methods=["GET"])
def login():
    if is_authenticated():
        return redirect(url_for("scan.index"))
    return render_template("login.html", error=None)


@bp.route("/login", methods=["POST"])
def login_submit():
    ip = _client_ip()
    if auth.is_locked(ip):
        return render_template(
            "login.html",
            error="Too many failed attempts. Try again later.",
        ), 429
    password = request.form.get("password", "")
    if auth.verify_password(password):
        auth.clear_failures(ip)
        session[_SESSION_KEY] = True
        session.permanent = True
        return redirect(_safe_next(request.args.get("next", "")))
    auth.record_failure(ip)
    return render_template("login.html", error="Incorrect password."), 401


@bp.route("/logout", methods=["POST"])
def logout():
    session.pop(_SESSION_KEY, None)
    return redirect(url_for("auth.login"))
