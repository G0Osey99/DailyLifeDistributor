"""Per-user 2FA management routes — /settings/2fa* (TOTP + email).

These routes are gated by `auth.login_required`. Disabling a method
requires proof you still control it (TOTP code for TOTP; email is
implicit since the session is the proof).

The pending-TOTP secret lives in the Flask session in encrypted form
between `enable-totp` and `verify-totp`, so even an attacker with read
access to the session blob can't extract the plaintext.
"""
from __future__ import annotations

from flask import (
    Blueprint, Response, flash, redirect, render_template, request, session,
    url_for,
)

from blueprints.auth import login_required
from core import audit as _audit
from core import db as _db
from core import email_2fa as _email_2fa
from core import recovery as _recovery
from core import totp as _totp
from core.qrcode_render import render_provisioning_qr_png

bp = Blueprint("twofa", __name__)


def _current_user() -> dict | None:
    uid = session.get("user_id")
    if uid is None:
        return None
    return _db.get_user_by_id(uid)


def _req_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")


def _req_ua() -> str:
    return request.headers.get("User-Agent", "")


@bp.get("/settings/2fa")
@login_required
def settings_2fa():
    user = _current_user() or {}
    return render_template(
        "settings_2fa.html",
        totp_enabled=bool(user.get("totp_enabled")),
        email_2fa_enabled=bool(user.get("email_2fa_enabled")),
    )


@bp.post("/settings/2fa/enable-totp")
@login_required
def enable_totp():
    user = _current_user() or {}
    secret = _totp.gen_secret()
    enc = _totp.encrypt_secret_for_storage(secret)
    session["pending_totp_secret_enc"] = enc
    uri = _totp.build_provisioning_uri(secret, user.get("username") or "user")
    session["pending_totp_uri"] = uri
    _audit.write_event(
        action="user.2fa_setup_started",
        actor_user_id=user.get("id"),
        metadata={"method": "totp"},
        ip=_req_ip(), ua=_req_ua(),
    )
    return render_template("settings_2fa_totp_setup.html")


@bp.get("/settings/2fa/qrcode.png")
@login_required
def totp_qrcode():
    uri = session.get("pending_totp_uri")
    if not uri:
        return ("", 404)
    return Response(render_provisioning_qr_png(uri), mimetype="image/png")


@bp.post("/settings/2fa/verify-totp")
@login_required
def verify_totp_post():
    user = _current_user() or {}
    enc = session.get("pending_totp_secret_enc")
    code = (request.form.get("code") or "").strip()
    if not enc:
        return ("Setup not started", 400)
    secret = _totp.decrypt_secret_from_storage(enc)
    if not secret or not _totp.verify_totp(secret, code):
        return render_template(
            "settings_2fa_totp_setup.html", error="Invalid code"
        ), 400
    _db.set_user_totp(user["id"], enc, enabled=True)
    session.pop("pending_totp_secret_enc", None)
    session.pop("pending_totp_uri", None)
    codes = _recovery.generate_recovery_codes(user["id"])
    _audit.write_event(
        action="user.2fa_enabled",
        actor_user_id=user["id"],
        metadata={"method": "totp"},
        ip=_req_ip(), ua=_req_ua(),
    )
    return render_template(
        "recovery_codes.html", codes=codes, first_time=True, remaining=10,
    )


@bp.post("/settings/2fa/enable-email")
@login_required
def enable_email_2fa():
    user = _current_user() or {}
    _db.set_user_email_2fa(user["id"], True)
    _email_2fa.generate_login_code(user["id"])
    _audit.write_event(
        action="user.2fa_enabled",
        actor_user_id=user["id"],
        metadata={"method": "email"},
        ip=_req_ip(), ua=_req_ua(),
    )
    flash("Email 2FA enabled. A test code was sent to your email.")
    return redirect(url_for("twofa.settings_2fa"))


@bp.post("/settings/2fa/send-email-code")
@login_required
def send_email_code():
    """Mint a fresh email 2FA code for the current user.

    Lets the user request a code they'll need to disable email 2FA
    (proof of factor possession). Only useful when email 2FA is
    already enabled — refuses otherwise.
    """
    user = _current_user()
    if not user or not user.get("email_2fa_enabled"):
        flash("Email 2FA is not enabled for this account.")
        return redirect(url_for("twofa.settings_2fa"))
    _email_2fa.generate_login_code(user["id"])
    flash("Sent a 6-digit code to your email. It expires in 10 minutes.")
    return redirect(url_for("twofa.settings_2fa"))


@bp.post("/settings/2fa/disable")
@login_required
def disable_2fa():
    user = _current_user() or {}
    method = (request.form.get("method") or "").strip()
    code = (request.form.get("code") or "").strip()
    if method == "totp":
        enc = user.get("totp_secret_encrypted")
        secret = _totp.decrypt_secret_from_storage(enc) if enc else None
        if not secret or not _totp.verify_totp(secret, code):
            return render_template(
                "settings_2fa.html",
                totp_enabled=True,
                email_2fa_enabled=bool(user.get("email_2fa_enabled")),
                error="Invalid code",
            ), 400
        _db.set_user_totp(user["id"], None, enabled=False)
    elif method == "email":
        # Disabling email 2FA must require proof of factor possession —
        # otherwise a hijacked session (XSS, stolen cookie, brief
        # unattended browser) can strip the second factor with one POST.
        # Verify a fresh emailed code, same posture as TOTP disable.
        if not user.get("email_2fa_enabled"):
            return ("Email 2FA is not enabled", 400)
        if not code or not _email_2fa.verify_login_code(user["id"], code):
            return render_template(
                "settings_2fa.html",
                totp_enabled=bool(user.get("totp_enabled")),
                email_2fa_enabled=True,
                error="Invalid or expired email code. Click 'Send code' first.",
            ), 400
        _db.set_user_email_2fa(user["id"], False)
    else:
        return ("Unknown method", 400)
    _audit.write_event(
        action="user.2fa_disabled",
        actor_user_id=user["id"],
        metadata={"method": method},
        ip=_req_ip(), ua=_req_ua(),
    )
    flash("Two-factor authentication disabled.")
    return redirect(url_for("twofa.settings_2fa"))


@bp.get("/settings/2fa/recovery-codes")
@login_required
def recovery_codes_view():
    user = _current_user() or {}
    codes = _db.list_recovery_codes(user["id"])
    remaining = sum(1 for c in codes if c["used_at"] is None)
    return render_template(
        "recovery_codes.html",
        codes=None,
        remaining=remaining,
        first_time=False,
    )


@bp.post("/settings/2fa/recovery-codes/regenerate")
@login_required
def recovery_codes_regenerate():
    user = _current_user() or {}
    codes = _recovery.regenerate_codes(user["id"])
    _audit.write_event(
        action="user.recovery_codes_regenerated",
        actor_user_id=user["id"],
        ip=_req_ip(), ua=_req_ua(),
    )
    return render_template(
        "recovery_codes.html", codes=codes, first_time=False, remaining=10,
    )
