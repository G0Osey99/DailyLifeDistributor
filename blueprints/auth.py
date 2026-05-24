"""Login/logout routes and the login_required decorator."""
from __future__ import annotations

import logging
import os
import urllib.parse
from functools import wraps

from flask import (
    Blueprint, current_app, redirect, render_template, request, session, url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from core import auth
from core.org_context import forbidden_during_impersonation

log = logging.getLogger(__name__)
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
        return url_for("scan.dashboard")
    parsed = urllib.parse.urlparse(nxt)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return url_for("scan.dashboard")
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


# Single source of truth for the legacy-mode flag lives in core.auth.
# Keep the underscored name here as a re-export so the existing template
# context + call sites in this module don't need to change.
_legacy_enabled = auth.legacy_enabled


@bp.route("/login", methods=["GET"])
def login():
    if is_authenticated():
        return redirect(url_for("scan.dashboard"))
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
            # Mitigate session fixation: rotate the session id by
            # clearing first, then mark authenticated. Matches the
            # modern user-id branch at line 143.
            session.clear()
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
    pw_ok = user is not None and user_store.password_matches(user["id"], password)
    if not pw_ok:
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

    # Forced first-login password change: the seed password matched, but
    # password_changed_at IS NULL — route to the "set your new password"
    # page instead of granting a session.
    if user_store.password_change_required(user["id"]):
        tok = _issue_partial_token(user["id"])
        return redirect(
            url_for("auth.first_password_set_get") + f"?tok={tok}"
        )
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
    except (BadSignature, SignatureExpired):
        # Tampered / expired / missing — caller treats as "go back to login".
        return None
    # Anything past the decode (KeyError on missing uid, ValueError on
    # non-int) is a bug, not a user error — let it surface so we see it.
    uid = data.get("uid") if isinstance(data, dict) else None
    if uid is None:
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        log.warning("partial token decoded but uid is not an int: %r", uid)
        return None


def _audit_event_safe(**kw) -> None:
    """Best-effort audit write — never propagate to the request handler."""
    try:
        from core import audit as _audit
        _audit.write_event(**kw)
    except Exception:
        # Audit writes mustn't block the request. Log so ops sees the
        # drop instead of compliance trail silently vanishing.
        log.warning("audit.write_event(%s) failed",
                    kw.get("action", "?"), exc_info=True)


def _notify_new_device_safe(user_id: int, ip: str, ua: str) -> None:
    try:
        from core import login_notifications as _ln
        _ln.notify_if_new_device(user_id, ip, ua)
    except Exception:
        log.warning(
            "new-device notification for user=%s ip=%s failed",
            user_id, ip, exc_info=True,
        )


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
        # last_login_at feeds the new-device-sighting heuristic; a silent
        # drop here means a real signal-of-compromise (login from a new
        # IP) could be missed because we never updated the row.
        log.warning(
            "update_last_login_at(user=%s) failed", user_id, exc_info=True,
        )
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
    # Brute-force guard: TOTP is 6 digits / 30s window — a 1000-rps
    # attacker with the partial token in hand would otherwise sweep
    # the keyspace inside a single drift window. The auth.* helpers
    # are reused so per-IP lockouts apply to both /login and the 2FA
    # second step under the same budget.
    ip = _client_ip()
    if auth.is_locked(ip):
        return render_template(
            "login_2fa.html", tok=tok,
            error="Too many failed attempts. Try again later.",
        ), 429
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
        auth.record_failure(ip)
        # Re-mint so the user can retry one more time without going back to /login.
        fresh_tok = _issue_partial_token(uid)
        return render_template(
            "login_2fa.html", tok=fresh_tok, error="Invalid code",
        ), 400
    auth.clear_failures(ip)
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
    # Brute-force guard — same posture as /login/2fa. 6-digit emailed
    # codes valid for 10 minutes are otherwise wide open to anyone who
    # captured the partial token (5min validity).
    ip = _client_ip()
    if auth.is_locked(ip):
        return render_template(
            "login_email_2fa.html", tok=tok,
            error="Too many failed attempts. Try again later.",
        ), 429
    code = (request.form.get("code") or "").strip()
    from core import email_2fa as _email_2fa
    if not _email_2fa.verify_login_code(uid, code):
        auth.record_failure(ip)
        # Re-issue a token so the form can retry without redirecting to /login.
        fresh_tok = _issue_partial_token(uid)
        return render_template(
            "login_email_2fa.html", tok=fresh_tok, error="Invalid code",
        ), 400
    auth.clear_failures(ip)
    _finalize_login(uid, "email")
    return redirect("/")


@bp.route("/login/first-password-set", methods=["GET"])
def first_password_set_get():
    tok = request.args.get("tok", "")
    uid = _consume_partial_token(tok)
    if uid is None:
        return redirect(url_for("auth.login"))
    # Re-mint so a refresh doesn't drop them.
    fresh_tok = _issue_partial_token(uid)
    return render_template("first_password_set.html", tok=fresh_tok, error=None)


@bp.route("/login/first-password-set", methods=["POST"])
@forbidden_during_impersonation
def first_password_set_post():
    tok = request.form.get("tok", "")
    uid = _consume_partial_token(tok)
    if uid is None:
        return redirect(url_for("auth.login"))
    new_pw = request.form.get("new_password", "") or ""
    confirm = request.form.get("confirm_password", "") or ""
    if new_pw != confirm:
        fresh_tok = _issue_partial_token(uid)
        return render_template(
            "first_password_set.html", tok=fresh_tok,
            error="Passwords do not match.",
        ), 400
    from core import passwords as _pw
    err = _pw.validate_password(new_pw)
    if err:
        fresh_tok = _issue_partial_token(uid)
        return render_template(
            "first_password_set.html", tok=fresh_tok, error=err,
        ), 400
    from core import user_store
    user_store.update_password(uid, new_pw)
    _audit_event_safe(
        action="user.password_changed",
        actor_user_id=uid,
        metadata={"reason": "first_login"},
        ip=_client_ip(),
        ua=request.headers.get("User-Agent", ""),
    )
    # Log them in (skip 2FA — they haven't enrolled yet on first login).
    _finalize_login(uid, "first_password_set")
    return redirect("/")


def _safe_referrer_redirect(default_endpoint: str):
    """Redirect to request.referrer, but only if it's same-origin.

    `Referer` is fully attacker-controlled (the browser will send whatever
    `Referrer-Policy` allows from a malicious source page). Bare
    `redirect(request.referrer)` is an open redirect; we run it through
    the same `_safe_next` validator the login flow uses.
    """
    target = urllib.parse.urlparse(request.referrer or "").path
    return redirect(_safe_next(target) or url_for(default_endpoint))


@bp.route("/account/switch_org", methods=["POST"])
def switch_org():
    if not is_authenticated():
        return redirect(url_for("auth.login"))
    try:
        new_org_id = int(request.form.get("org_id") or 0)
    except ValueError:
        new_org_id = 0
    if not new_org_id:
        return _safe_referrer_redirect("scan.dashboard")
    from core import org_store
    uid = auth.current_user_id()
    mem = org_store.get_membership(user_id=uid, org_id=new_org_id)
    if mem is None:
        from flask import abort
        abort(403)
    session["current_org_id"] = new_org_id
    return _safe_referrer_redirect("scan.dashboard")
