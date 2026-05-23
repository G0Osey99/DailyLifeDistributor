"""Login/logout routes and the login_required decorator."""
from __future__ import annotations

import os
import urllib.parse
from functools import wraps

from flask import (
    Blueprint, current_app, redirect, render_template, request, session, url_for,
)
from itsdangerous import URLSafeTimedSerializer

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
    return auth.is_authenticated()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _legacy_enabled() -> bool:
    return (os.environ.get("LEGACY_PASSWORD_ENABLED", "") or "").lower() in (
        "1", "true", "yes",
    )


@bp.route("/login", methods=["GET"])
def login():
    if is_authenticated():
        return redirect(url_for("scan.index"))
    return render_template(
        "login.html", error=None, legacy_enabled=_legacy_enabled(),
    )


@bp.route("/login", methods=["POST"])
def login_submit():
    ip = _client_ip()
    if auth.is_locked(ip):
        return render_template(
            "login.html",
            error="Too many failed attempts. Try again later.",
            legacy_enabled=_legacy_enabled(),
        ), 429

    # Legacy path: the old shared-password form posts only a `password`
    # field. We keep accepting it for one release behind LEGACY_PASSWORD_ENABLED.
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password", "") or ""

    if not username and _legacy_enabled():
        if auth.verify_password(password):  # old shared-password verify
            auth.clear_failures(ip)
            session["authenticated"] = True
            session.permanent = True
            return redirect(_safe_next(request.args.get("next", "")))
        auth.record_failure(ip)
        return render_template(
            "login.html", error="Incorrect password.",
            legacy_enabled=True,
        ), 401

    # New path: username + password (Argon2id).
    from core import user_store, org_store
    user = user_store.get_user_by_username(username)
    if user is None or not user_store.verify_password(user["id"], password):
        auth.record_failure(ip)
        _audit_event_safe(
            action="user.login_failed",
            metadata={"username": username},
            ip=ip,
            ua=request.headers.get("User-Agent", ""),
        )
        return render_template(
            "login.html", error="Incorrect username or password.",
            legacy_enabled=_legacy_enabled(),
        ), 401

    auth.clear_failures(ip)
    # Phase γ: if the user has any 2FA enabled, hold the session as
    # "password-verified but 2FA-pending" and redirect to the second step.
    if user.get("totp_enabled") or user.get("email_2fa_enabled"):
        tok = _issue_partial_token(user["id"])
        if user.get("totp_enabled"):
            return redirect(url_for("auth.login_2fa_get") + f"?tok={tok}")
        return redirect(url_for("auth.login_email_2fa_get") + f"?tok={tok}")
    # No 2FA — finalize session immediately.
    session.clear()
    session["user_id"] = user["id"]
    mems = org_store.list_memberships_for_user(user["id"])
    session["current_org_id"] = mems[0]["org_id"] if mems else None
    session.permanent = True
    user_store.update_last_login_at(user["id"])
    _audit_event_safe(
        action="user.login",
        actor_user_id=user["id"],
        ip=ip,
        ua=request.headers.get("User-Agent", ""),
    )
    _notify_new_device_safe(user["id"], ip, request.headers.get("User-Agent", ""))
    return redirect(_safe_next(request.args.get("next", "")))


@bp.route("/logout", methods=["POST"])
def logout():
    uid = session.get("user_id")
    if uid:
        _audit_event_safe(
            action="user.logout",
            actor_user_id=uid,
            ip=_client_ip(),
            ua=request.headers.get("User-Agent", ""),
        )
    session.pop(_SESSION_KEY, None)
    session.pop("user_id", None)
    session.pop("current_org_id", None)
    return redirect(url_for("auth.login"))


# ---------- Phase γ: 2FA second-step + new-device notifications ----------

def _issue_partial_token(user_id: int) -> str:
    s = URLSafeTimedSerializer(current_app.secret_key, salt="2fa-pending")
    return s.dumps({"uid": user_id})


def _consume_partial_token(tok: str) -> int | None:
    s = URLSafeTimedSerializer(current_app.secret_key, salt="2fa-pending")
    try:
        data = s.loads(tok, max_age=300)
        return int(data["uid"])
    except Exception:
        return None


def _audit_event_safe(**kw) -> None:
    """Best-effort audit write — never propagate to the request handler."""
    try:
        from core import audit as _audit
        _audit.write_event(**kw)
    except Exception:  # pragma: no cover
        pass


def _notify_new_device_safe(user_id: int, ip: str, ua: str) -> None:
    try:
        from core import login_notifications as _ln
        _ln.notify_if_new_device(user_id, ip, ua)
    except Exception:  # pragma: no cover
        pass


def _finalize_login(user_id: int, second_factor: str) -> None:
    """Mint the post-2FA session + emit audit + new-device email."""
    from core import org_store, user_store
    session.clear()
    session["user_id"] = user_id
    mems = org_store.list_memberships_for_user(user_id)
    session["current_org_id"] = mems[0]["org_id"] if mems else None
    session.permanent = True
    try:
        user_store.update_last_login_at(user_id)
    except Exception:
        pass
    _audit_event_safe(
        action="user.login",
        actor_user_id=user_id,
        metadata={"second_factor": second_factor},
        ip=_client_ip(),
        ua=request.headers.get("User-Agent", ""),
    )
    _notify_new_device_safe(
        user_id, _client_ip(), request.headers.get("User-Agent", ""),
    )


@bp.route("/login/2fa", methods=["GET"])
def login_2fa_get():
    tok = request.args.get("tok", "")
    uid = _consume_partial_token(tok)
    if uid is None:
        return redirect(url_for("auth.login"))
    return render_template("login_2fa.html", tok=tok)


@bp.route("/login/2fa", methods=["POST"])
def login_2fa_post():
    tok = request.form.get("tok", "")
    uid = _consume_partial_token(tok)
    if uid is None:
        return redirect(url_for("auth.login"))
    code = (request.form.get("code") or "").strip()
    from core import db as _db
    from core import recovery as _recovery
    from core import totp as _totp
    user = _db.get_user_by_id(uid)
    enc = user.get("totp_secret_encrypted") if user else None
    secret = _totp.decrypt_secret_from_storage(enc) if enc else None
    used = None
    if secret and _totp.verify_totp(secret, code):
        used = "totp"
    elif _recovery.verify_recovery_code(uid, code):
        used = "recovery_code"
    if not used:
        return render_template("login_2fa.html", tok=tok, error="Invalid code"), 400
    _finalize_login(uid, used)
    return redirect("/")


@bp.route("/login/email-2fa", methods=["GET"])
def login_email_2fa_get():
    tok = request.args.get("tok", "")
    uid = _consume_partial_token(tok)
    if uid is None:
        return redirect(url_for("auth.login"))
    # Re-mint the partial token so a refresh of this page doesn't drop the
    # user back to /login (the token is single-use by intent of consume()).
    fresh_tok = _issue_partial_token(uid)
    from core import email_2fa as _email_2fa
    _email_2fa.generate_login_code(uid)
    return render_template("login_email_2fa.html", tok=fresh_tok)


@bp.route("/login/email-2fa", methods=["POST"])
def login_email_2fa_post():
    tok = request.form.get("tok", "")
    uid = _consume_partial_token(tok)
    if uid is None:
        return redirect(url_for("auth.login"))
    code = (request.form.get("code") or "").strip()
    from core import email_2fa as _email_2fa
    if not _email_2fa.verify_login_code(uid, code):
        # Re-issue a token so the form can retry without redirecting to /login.
        fresh_tok = _issue_partial_token(uid)
        return render_template(
            "login_email_2fa.html", tok=fresh_tok, error="Invalid code",
        ), 400
    _finalize_login(uid, "email")
    return redirect("/")


@bp.route("/account/switch_org", methods=["POST"])
def switch_org():
    if not is_authenticated():
        return redirect(url_for("auth.login"))
    try:
        new_org_id = int(request.form.get("org_id") or 0)
    except ValueError:
        new_org_id = 0
    if not new_org_id:
        return redirect(request.referrer or url_for("scan.index"))
    from core import org_store
    uid = auth.current_user_id()
    mem = org_store.get_membership(user_id=uid, org_id=new_org_id)
    if mem is None:
        from flask import abort
        abort(403)
    session["current_org_id"] = new_org_id
    return redirect(request.referrer or url_for("scan.index"))
