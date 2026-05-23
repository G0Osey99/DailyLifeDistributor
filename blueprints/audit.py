"""Audit-log views — org-scoped at /settings/audit-log."""
from __future__ import annotations

from flask import Blueprint, abort, render_template, request, session

from blueprints.auth import login_required
from core import db as _db

bp = Blueprint("audit", __name__)


def _user_role_in_org(user_id: int, org_id: int) -> str | None:
    m = _db.get_membership(user_id, org_id)
    return m["role"] if m else None


@bp.get("/settings/audit-log")
@login_required
def audit_log_view():
    uid = session.get("user_id")
    org_id = session.get("current_org_id")
    if not org_id:
        abort(400)
    role = _user_role_in_org(uid, org_id)
    if role not in ("owner", "manager"):
        abort(403)
    action_prefix = request.args.get("action") or None
    actor = request.args.get("actor")
    actor_id = int(actor) if actor and actor.isdigit() else None
    since = request.args.get("since") or None
    until = request.args.get("until") or None
    rows = _db.list_audit_events(
        org_id=org_id,
        action_prefix=action_prefix,
        actor_user_id=actor_id,
        since=since,
        until=until,
        limit=500,
    )
    return render_template(
        "audit_log.html",
        rows=rows,
        filters={
            "action": action_prefix,
            "actor": actor_id,
            "since": since,
            "until": until,
        },
        cross_org=False,
    )
