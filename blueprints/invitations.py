"""Invitation send + accept routes.

Send-side: POST /settings/members/invite, POST /settings/members/<id>/revoke
  Role gate: owner OR manager. Manager can only invite User-role and can
  only revoke User-role invites OR their own pending invites.
  Spam guard: 3 pending invites per email per org.

Accept-side: GET /invite/accept?token=... + POST /invite/accept
  Validates the signed token, creates a user + membership, logs in, and
  sends the welcome email. Username & password policy enforced.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template,
    request, session, url_for,
)

from core import db, email as email_mod, invitations, passwords, user_store
from core.permissions import _lookup_role, require_role

log = logging.getLogger(__name__)

bp = Blueprint("invitations", __name__)

_MAX_PENDING_PER_EMAIL = 3
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")

_AGENT_WIN_URL = os.environ.get(
    "AGENT_WIN_DOWNLOAD_URL",
    "https://autoalert.pro/download/agent/windows",
)
_AGENT_MAC_URL = os.environ.get(
    "AGENT_MAC_DOWNLOAD_URL",
    "https://autoalert.pro/download/agent/macos",
)
_DASHBOARD_URL = os.environ.get(
    "DLD_DASHBOARD_URL", "https://autoalert.pro/",
)


def _inviter_username(user_id: int) -> str:
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    return row["username"] if row else "Someone"


def _org_name(org_id: int) -> str:
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM organizations WHERE id = ?", (org_id,),
        ).fetchone()
    return row["name"] if row else "your organization"


def _members_redirect():
    if "members.members_page" in current_app.view_functions:
        return redirect(url_for("members.members_page"))
    return redirect("/settings/members")


@bp.route("/settings/members/invite", methods=["POST"])
@require_role("owner", "manager")
def send_invite():
    email_addr = (request.form.get("email") or "").strip().lower()
    role = (request.form.get("role") or "user").strip().lower()
    org_id_raw = session.get("current_org_id")
    user_id_raw = session.get("user_id")

    if not org_id_raw or not user_id_raw:
        # Legacy session (LEGACY_PASSWORD_ENABLED) has neither but already
        # passed the role gate. Invites are a multi-tenant feature — refuse
        # to send unscoped invitations.
        flash("Invitations require a current organization.", "error")
        return _members_redirect()

    org_id = int(org_id_raw)
    user_id = int(user_id_raw)

    if not email_addr or "@" not in email_addr:
        flash("Enter a valid email address.", "error")
        return _members_redirect()
    if role not in ("owner", "manager", "user"):
        flash("Invalid role.", "error")
        return _members_redirect()

    # Manager can only invite Users.
    actor_role = _lookup_role(user_id, org_id)
    if actor_role == "manager" and role != "user":
        flash("Managers can only invite Users.", "error")
        return _members_redirect()

    # Spam guard: at most N pending invites per email per org.
    pending = invitations.list_invitations_by_email(
        email_addr, org_id, status="pending",
    )
    if len(pending) >= _MAX_PENDING_PER_EMAIL:
        flash(
            f"There are already {len(pending)} pending invites for {email_addr}.",
            "error",
        )
        return _members_redirect()

    inv_id, raw_token = invitations.create_invitation(
        org_id=org_id, inviter_user_id=user_id,
        email=email_addr, role=role,
    )
    accept_url = url_for(
        "invitations.accept_get", token=raw_token, _external=True,
    )
    try:
        email_mod.send(
            "invite", to=email_addr,
            org_name=_org_name(org_id),
            inviter_name=_inviter_username(user_id),
            role=role,
            accept_url=accept_url,
            agent_win_url=_AGENT_WIN_URL,
            agent_mac_url=_AGENT_MAC_URL,
        )
    except Exception:
        # Don't surface the send-side error to the inviter — the row is
        # already in the DB; the recipient can be re-sent later.
        log.exception("invite send failed for %s", email_addr)
    flash(f"Invitation sent to {email_addr}.", "success")
    return _members_redirect()


@bp.route("/settings/members/<int:invitation_id>/revoke", methods=["POST"])
@require_role("owner", "manager")
def revoke(invitation_id: int):
    org_id_raw = session.get("current_org_id")
    user_id_raw = session.get("user_id")
    if not org_id_raw or not user_id_raw:
        flash("Revoke requires a current organization.", "error")
        return _members_redirect()
    org_id = int(org_id_raw)
    user_id = int(user_id_raw)

    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT org_id, inviter_user_id, role FROM invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
    if not row or int(row["org_id"]) != org_id:
        abort(404)
    actor_role = _lookup_role(user_id, org_id)
    # Manager can only revoke invites they created OR User-role invites.
    if (
        actor_role == "manager"
        and int(row["inviter_user_id"]) != user_id
        and row["role"] != "user"
    ):
        abort(403)
    invitations.revoke_invitation(invitation_id)
    flash("Invitation revoked.", "success")
    return _members_redirect()


# -------- accept-side --------------------------------------------------

def _load_invitation_for_token(raw_token: str):
    inv_id = invitations.verify_token(raw_token)
    if inv_id is None:
        return None, "Invalid or expired token."
    inv = invitations.get_invitation_with_org(inv_id)
    if not inv:
        return None, "Invitation not found."
    if inv["accepted_at"]:
        return None, "This invitation has already been accepted."
    if inv["revoked_at"]:
        return None, "This invitation has been revoked."
    return inv, None


@bp.route("/invite/accept", methods=["GET"])
def accept_get():
    token = request.args.get("token", "")
    inv, err = _load_invitation_for_token(token)
    if err:
        return (
            render_template("invite_accept.html",
                            error=err, invitation=None),
            400,
        )
    return render_template(
        "invite_accept.html",
        error=None, invitation=inv, token=token,
    )


@bp.route("/invite/accept", methods=["POST"])
def accept_post():
    token = (request.form.get("token") or "").strip()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    inv, err = _load_invitation_for_token(token)
    if err:
        return (
            render_template("invite_accept.html",
                            error=err, invitation=None),
            400,
        )
    if not _USERNAME_RE.match(username):
        return (
            render_template(
                "invite_accept.html",
                error=None, invitation=inv, token=token,
                form_error="Username must be 3-32 chars: A-Z, a-z, 0-9, _ or -.",
            ),
            400,
        )
    pw_err = passwords.validate_password(password)
    if pw_err:
        return (
            render_template(
                "invite_accept.html",
                error=None, invitation=inv, token=token,
                form_error=pw_err,
            ),
            400,
        )
    # Uniqueness checks happen at create time; user_store.create_user
    # raises IntegrityError on collision.
    existing_u = user_store.get_user_by_username(username)
    if existing_u is not None:
        return (
            render_template(
                "invite_accept.html",
                error=None, invitation=inv, token=token,
                form_error="Username already taken.",
            ),
            400,
        )
    new_user = user_store.create_user(
        username=username, email=inv["email"], password=password,
    )
    # password_changed_at is NULL after create_user (forces a change). The
    # invite path is the change — set it immediately so the new user can
    # actually log in.
    user_store.update_password(new_user["id"], password)
    ok = invitations.accept_invitation(int(inv["id"]), int(new_user["id"]))
    if not ok:
        # Race: invitation was revoked / accepted between page load and POST.
        abort(409)
    session.clear()
    session["user_id"] = int(new_user["id"])
    session["current_org_id"] = int(inv["org_id"])
    session.permanent = True
    user_store.update_last_login_at(int(new_user["id"]))
    try:
        email_mod.send(
            "welcome", to=inv["email"],
            org_name=inv["org_name"], username=username,
            role=inv["role"],
            dashboard_url=_DASHBOARD_URL,
            agent_win_url=_AGENT_WIN_URL,
            agent_mac_url=_AGENT_MAC_URL,
        )
    except Exception:
        log.exception("welcome send failed for %s", inv["email"])
    if "scan.index" in current_app.view_functions:
        return redirect(url_for("scan.index"))
    return redirect("/")
