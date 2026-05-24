"""Per-org member management: list, change role, remove.

Role rules:
  * GET /settings/members         — owner OR manager
  * POST .../role                 — owner only
  * POST .../remove               — owner OR manager; manager limited to Users
  * Sole-owner safety: an Owner cannot demote / remove themselves if they
    are the only Owner in the org.
"""
from __future__ import annotations

from flask import (
    Blueprint, abort, flash, redirect, render_template,
    request, session, url_for,
)

from core import audit as _audit
from core import db, invitations
from core.permissions import _lookup_role, require_role


def _req_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")


def _req_ua() -> str:
    return request.headers.get("User-Agent", "")

bp = Blueprint("members", __name__)


def _list_members(org_id: int) -> list[dict]:
    with db._get_conn() as conn:
        rows = conn.execute(
            """SELECT u.id, u.username, u.email, m.role,
                      m.joined_at, u.last_login_at
               FROM org_memberships m
               JOIN users u ON u.id = m.user_id
               WHERE m.org_id = ?
               ORDER BY m.joined_at ASC""",
            (org_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _owner_count(org_id: int) -> int:
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM org_memberships "
            "WHERE org_id = ? AND role = 'owner'",
            (org_id,),
        ).fetchone()
    return int(row["c"]) if row else 0


@bp.route("/settings/members", methods=["GET"])
@require_role("owner", "manager")
def members_page():
    org_id_raw = session.get("current_org_id")
    user_id_raw = session.get("user_id")
    if not org_id_raw or not user_id_raw:
        # Legacy single-tenant session — no org concept. Render an empty page.
        return render_template(
            "members.html",
            members=[], pending=[], actor_role="owner",
        )
    org_id = int(org_id_raw)
    user_id = int(user_id_raw)
    actor_role = _lookup_role(user_id, org_id) or "owner"
    return render_template(
        "members.html",
        members=_list_members(org_id),
        pending=invitations.list_pending_invitations(org_id),
        actor_role=actor_role,
    )


@bp.route("/settings/members/<int:user_id>/role", methods=["POST"])
@require_role("owner")
def change_role(user_id: int):
    new_role = (request.form.get("role") or "").strip().lower()
    if new_role not in ("owner", "manager", "user"):
        abort(400)
    org_id_raw = session.get("current_org_id")
    actor_id_raw = session.get("user_id")
    if not org_id_raw or not actor_id_raw:
        abort(400)
    org_id = int(org_id_raw)
    actor_id = int(actor_id_raw)
    with db._get_conn() as conn:
        target = conn.execute(
            "SELECT role FROM org_memberships "
            "WHERE user_id = ? AND org_id = ?",
            (user_id, org_id),
        ).fetchone()
    if not target:
        abort(404)
    # Sole-owner guard.
    if (
        user_id == actor_id
        and target["role"] == "owner"
        and new_role != "owner"
        and _owner_count(org_id) <= 1
    ):
        flash(
            "You're the only Owner — promote someone else first.",
            "error",
        )
        return ("Sole-owner demotion blocked", 400)
    old_role = target["role"]
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE org_memberships SET role = ? "
            "WHERE user_id = ? AND org_id = ?",
            (new_role, user_id, org_id),
        )
        conn.commit()
    _audit.write_event(
        action="org.role_changed",
        actor_user_id=actor_id, org_id=org_id,
        target_type="user", target_id=user_id,
        metadata={"from": old_role, "to": new_role},
        ip=_req_ip(), ua=_req_ua(),
    )
    flash("Role updated.", "success")
    return redirect(url_for("members.members_page"))


@bp.route("/settings/members/<int:user_id>/remove", methods=["POST"])
@require_role("owner", "manager")
def remove_member(user_id: int):
    org_id_raw = session.get("current_org_id")
    actor_id_raw = session.get("user_id")
    if not org_id_raw or not actor_id_raw:
        abort(400)
    org_id = int(org_id_raw)
    actor_id = int(actor_id_raw)
    actor_role = _lookup_role(actor_id, org_id)
    with db._get_conn() as conn:
        target = conn.execute(
            "SELECT role FROM org_memberships "
            "WHERE user_id = ? AND org_id = ?",
            (user_id, org_id),
        ).fetchone()
    if not target:
        abort(404)
    # Manager can only remove Users.
    if actor_role == "manager" and target["role"] != "user":
        abort(403)
    # Owner can't remove themselves if sole Owner.
    if (
        user_id == actor_id
        and target["role"] == "owner"
        and _owner_count(org_id) <= 1
    ):
        flash(
            "You're the only Owner — promote someone else first.",
            "error",
        )
        return ("Sole-owner removal blocked", 400)
    with db._get_conn() as conn:
        conn.execute(
            "DELETE FROM org_memberships "
            "WHERE user_id = ? AND org_id = ?",
            (user_id, org_id),
        )
        conn.commit()
    _audit.write_event(
        action="org.member_removed",
        actor_user_id=actor_id, org_id=org_id,
        target_type="user", target_id=user_id,
        ip=_req_ip(), ua=_req_ua(),
    )
    flash("Member removed.", "success")
    return redirect(url_for("members.members_page"))
