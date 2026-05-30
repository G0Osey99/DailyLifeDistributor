"""Account-recovery routes — submit, approve, reset.

Three endpoints:
  * GET/POST /recover                          — public form
  * GET /admin-actions/recovery/<id>/approve   — Owner one-click approval
  * GET/POST /recover/reset                    — public token-gated new password
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

from flask import (
    Blueprint, abort, current_app, redirect, render_template, request,
    session, url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from blueprints.auth import login_required
from core import audit as _audit
from core.org_context import forbidden_during_impersonation
from core import db as _db
from core import email as _email
from core import recovery_request as _rreq
from core import user_store as _user_store

bp = Blueprint("recovery", __name__)


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="recovery-reset")


def _base_url() -> str:
    try:
        return current_app.config.get("BASE_URL", "https://autoalert.pro")
    except RuntimeError:
        return "https://autoalert.pro"


def _request_expired(rrow: dict) -> bool:
    """SEC-003: a recovery request has a 48h TTL (recovery_request._REQ_TTL),
    written to expires_at at submit time but previously never enforced — so an
    Owner could approve a months-old request and the reset link was bounded
    only by the 1h token age minted at approval. Treat an unparseable / NULL
    expires_at (legacy pre-column rows) as not-expired so in-flight requests
    aren't broken; the approved/consumed guards still apply."""
    raw = rrow.get("expires_at")
    if not raw:
        return False
    try:
        exp = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < datetime.now(timezone.utc)


@bp.get("/recover")
def recover_form():
    return render_template("recover.html")


@bp.post("/recover")
def recover_submit():
    username = (request.form.get("username") or "").strip()
    note = (request.form.get("note") or "").strip()[:1000]
    _rreq.submit_request(username, note)
    # Generic message — no enumeration.
    return render_template(
        "recover.html",
        message="If this account exists, your organization's owners have been notified.",
    )


@bp.get("/admin-actions/recovery/<int:request_id>/approve")
@forbidden_during_impersonation
@login_required
def approve(request_id: int):
    rrow = _db.get_recovery_request(request_id)
    if not rrow:
        abort(404)
    if rrow.get("approved_at") or rrow.get("consumed_at"):
        return render_template(
            "recover.html",
            message="This request has already been processed.",
        )
    if _request_expired(rrow):
        return render_template(
            "recover.html",
            message="This recovery request has expired. Ask the user to submit a new one.",
        )
    approver_id = session.get("user_id")
    if not approver_id or not _db.user_owns_any_org_with(
        approver_id, rrow["user_id"]
    ):
        abort(403)
    # Privilege-escalation guard: an org Owner who shares a room with the
    # program-owner could otherwise approve a forged recovery request for
    # them and take over the master account (including the program_owner
    # flag, since recovery also wipes 2FA). Only a program-owner may
    # approve recovery for another program-owner.
    target = _db.get_user_by_id(rrow["user_id"])
    if target and target.get("program_owner"):
        approver = _db.get_user_by_id(approver_id)
        if not approver or not approver.get("program_owner"):
            log.warning(
                "recovery.approve: %s (uid=%s) attempted to approve "
                "recovery for program-owner (uid=%s) — refused",
                (_db.get_user_by_id(approver_id) or {}).get("username"),
                approver_id, rrow["user_id"],
            )
            abort(403)
    now = datetime.now(timezone.utc).isoformat()
    token = _reset_serializer().dumps(
        {"uid": rrow["user_id"], "rid": request_id}
    )
    _db.update_recovery_request_approve(request_id, approver_id, now, token)
    requester = _db.get_user_by_id(rrow["user_id"])
    reset_url = _base_url() + url_for("recovery.reset_form") + f"?token={token}"
    approver = _db.get_user_by_id(approver_id)
    _email.send(
        "recovery_approved",
        to=requester["email"],
        approver_username=approver["username"] if approver else "an Owner",
        reset_url=reset_url,
    )
    _audit.write_event(
        action="user.recovery_approved",
        actor_user_id=approver_id,
        target_type="user", target_id=requester["id"],
        metadata={"request_id": request_id},
    )
    return render_template(
        "recover.html",
        message=(
            f"Recovery approved. An email was sent to {requester['email']}."
        ),
    )


@bp.get("/recover/reset")
def reset_form():
    return render_template("recover_reset.html", token=request.args.get("token", ""))


@bp.post("/recover/reset")
def reset_submit():
    token = request.values.get("token", "")
    pw = request.form.get("password", "")
    pw2 = request.form.get("password2", "")
    if pw != pw2 or len(pw) < 12:
        return render_template(
            "recover_reset.html",
            token=token,
            error="Passwords must match and be at least 12 chars.",
        ), 400
    try:
        data = _reset_serializer().loads(token, max_age=3600)  # 1 hour
    except (BadSignature, SignatureExpired):
        # Narrow except: anything other than tamper/expiry is a server
        # bug and should surface, not get swallowed as "token invalid".
        return render_template(
            "recover_reset.html",
            token=token,
            error="Token expired or invalid.",
        ), 400
    if not isinstance(data, dict) or "rid" not in data or "uid" not in data:
        return render_template(
            "recover_reset.html",
            token=token,
            error="Token payload is malformed.",
        ), 400
    rid = data["rid"]
    uid = data["uid"]
    rrow = _db.get_recovery_request(rid)
    if not rrow or rrow.get("consumed_at"):
        return render_template(
            "recover_reset.html",
            token=token,
            error="Token already used.",
        ), 400
    # NOTE: the 48h request expiry (_request_expired) is enforced at APPROVE
    # time, not here. The reset token has its own independent 1-hour max_age
    # (loads() above), so an approval issued late in the 48h window still
    # yields a usable 1h reset link; re-checking expires_at here would reject
    # a legitimately-approved reset whose token is still valid.
    h = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if rrow.get("password_reset_token_hash") != h:
        abort(400)
    _user_store.update_password(uid, pw)
    _db.set_user_totp(uid, None, enabled=False)
    _db.set_user_email_2fa(uid, False)
    _db.delete_recovery_codes(uid)
    _db.consume_recovery_request(rid)
    _audit.write_event(
        action="user.password_changed",
        actor_user_id=uid,
        metadata={"via": "recovery"},
    )
    return redirect(url_for("auth.login"))
