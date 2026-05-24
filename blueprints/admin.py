"""Program-owner admin (multi-tenant phase α).

Routes here are gated by ``users.program_owner = TRUE``. The user-list
page exposes a force-password-reset action that flips
``password_changed_at = NULL`` so the target is required to set a new
password via ``/login/first-password-set`` on next login. The owner-self
and cross-owner cases are guarded: a program-owner cannot force-reset
another program-owner (privilege escalation), and the action is
audit-logged as ``user.force_password_reset``.
"""
from __future__ import annotations

import re

from flask import (
    Blueprint, redirect, render_template, request, url_for,
)

from core import org_store, auth
from core.permissions import require_program_owner

bp = Blueprint("admin", __name__, url_prefix="/admin")


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "org"


@bp.route("/", methods=["GET"])
@bp.route("", methods=["GET"])
@require_program_owner
def landing():
    # /admin is just the entry point — point it at the org list so it
    # doesn't render the same page under two URLs.
    return redirect(url_for("admin.organizations_list"))


@bp.route("/organizations", methods=["GET"])
@require_program_owner
def organizations_list():
    orgs = org_store.list_orgs()
    return render_template(
        "admin/organizations.html",
        orgs=orgs,
        form_error=None,
        landing=False,
    )


@bp.route("/organizations", methods=["POST"])
@require_program_owner
def organizations_create():
    name = (request.form.get("name") or "").strip()
    slug = (request.form.get("slug") or "").strip().lower()
    if not name:
        orgs = org_store.list_orgs()
        return render_template(
            "admin/organizations.html",
            orgs=orgs,
            form_error="Org name is required.",
            landing=False,
        ), 400
    if not slug:
        slug = _slugify(name)
    if not _SLUG_RE.match(slug):
        orgs = org_store.list_orgs()
        return render_template(
            "admin/organizations.html",
            orgs=orgs,
            form_error="Slug must be lowercase letters, digits, and dashes.",
            landing=False,
        ), 400
    if org_store.get_org_by_slug(slug):
        orgs = org_store.list_orgs()
        return render_template(
            "admin/organizations.html",
            orgs=orgs,
            form_error=f"Slug {slug!r} already exists.",
            landing=False,
        ), 400
    new_org = org_store.create_org(
        name=name, slug=slug,
        created_by_user_id=auth.current_user_id(),
    )
    try:
        from core import audit as _audit
        # `create_org` returns either an int or a dict depending on phase;
        # handle both shapes defensively.
        new_id = new_org if isinstance(new_org, int) else (new_org or {}).get("id")
        _audit.write_event(
            action="org.created",
            actor_user_id=auth.current_user_id(),
            org_id=new_id,
            target_type="org", target_id=new_id,
            metadata={"name": name, "slug": slug},
            ip=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            ua=request.headers.get("User-Agent", ""),
        )
    except Exception:
        # Audit-log write failed (DB hiccup, schema drift). Don't block
        # the org creation — it's already committed — but make the drop
        # visible to ops so we don't lose compliance trail silently.
        import logging
        logging.getLogger(__name__).exception(
            "audit.write_event(org.created) failed for slug=%r", slug,
        )
    return redirect(url_for("admin.organizations_list"))


@bp.route("/organizations/<int:org_id>", methods=["GET"])
@require_program_owner
def organization_detail(org_id: int):
    """Admin view of one org: members + pending invites + invite form."""
    from core import db as _db, invitations as _inv
    from flask import abort
    org = org_store.get_org_by_id(org_id)
    if not org:
        abort(404)
    with _db._get_conn() as c:
        members = [dict(r) for r in c.execute(
            """SELECT u.id, u.username, u.email, m.role, m.joined_at,
                      u.last_login_at, u.program_owner
               FROM org_memberships m
               JOIN users u ON u.id = m.user_id
               WHERE m.org_id = ?
               ORDER BY m.role, m.joined_at""",
            (org_id,),
        ).fetchall()]
    pending = _inv.list_pending_invitations(org_id)
    return render_template(
        "admin/organization_detail.html",
        org=org, members=members, pending=pending,
        form_error=None,
    )


@bp.route("/organizations/<int:org_id>/invite", methods=["POST"])
@require_program_owner
def organization_invite(org_id: int):
    """Program-owner invites a user into any org."""
    from core import invitations as _inv, email as _email
    from flask import abort, current_app, flash
    org = org_store.get_org_by_id(org_id)
    if not org:
        abort(404)
    email_addr = (request.form.get("email") or "").strip().lower()
    role = (request.form.get("role") or "user").strip().lower()
    if not email_addr or "@" not in email_addr:
        flash("Enter a valid email address.", "error")
        return redirect(url_for("admin.organization_detail", org_id=org_id))
    if role not in ("owner", "manager", "user"):
        flash("Invalid role.", "error")
        return redirect(url_for("admin.organization_detail", org_id=org_id))
    inv_id, raw_token = _inv.create_invitation(
        org_id=org_id, inviter_user_id=auth.current_user_id(),
        email=email_addr, role=role,
    )
    accept_url = url_for(
        "invitations.accept_get", token=raw_token, _external=True,
    )

    def _opt(endpoint: str, **kwargs) -> str:
        if endpoint in current_app.view_functions:
            return url_for(endpoint, _external=True, **kwargs)
        return ""

    try:
        _email.send(
            "invite", to=email_addr,
            org_name=org["name"],
            inviter_name="Program owner",
            role=role,
            accept_url=accept_url,
            agent_win_url=_opt("download.windows"),
            agent_mac_url=_opt("download.macos"),
        )
        flash(f"Invitation sent to {email_addr}. (Accept URL: {accept_url})", "success")
    except Exception as e:
        # Email send failed (no Resend key, network blip, etc). The invite
        # row is committed — surface the accept URL so the admin can
        # forward it manually until Resend is configured.
        flash(
            f"Invite created but email send failed ({e}). "
            f"Accept URL: {accept_url}",
            "warning",
        )
    return redirect(url_for("admin.organization_detail", org_id=org_id))


@bp.route("/organizations/<int:org_id>/invitations/<int:invitation_id>/revoke", methods=["POST"])
@require_program_owner
def organization_revoke_invite(org_id: int, invitation_id: int):
    from core import invitations as _inv
    from flask import flash
    _inv.revoke_invitation(invitation_id)
    flash("Invitation revoked.", "success")
    return redirect(url_for("admin.organization_detail", org_id=org_id))


@bp.route("/users", methods=["GET"])
@require_program_owner
def users_list():
    from core import db as _db
    with _db._get_conn() as c:
        rows = c.execute(
            "SELECT id, username, email, program_owner, created_at, "
            "last_login_at, password_changed_at "
            "FROM users ORDER BY created_at"
        ).fetchall()
    users = [dict(r) for r in rows]
    return render_template("admin/users.html", users=users, notice=None)


@bp.route("/audit-log", methods=["GET"])
@require_program_owner
def admin_audit_log():
    """Cross-org audit-log search for program-owners.

    Same template as /settings/audit-log but with `cross_org=True` so the
    UI omits the org-id filter (events from every org are interleaved by
    timestamp).
    """
    from core import db as _db
    action_prefix = request.args.get("action") or None
    org_id = request.args.get("org_id")
    org_id_int = int(org_id) if org_id and org_id.isdigit() else None
    since = request.args.get("since") or None
    until = request.args.get("until") or None
    rows = _db.list_audit_events(
        org_id=org_id_int, action_prefix=action_prefix,
        since=since, until=until, limit=1000,
    )
    return render_template(
        "audit_log.html",
        rows=rows,
        filters={
            "action": action_prefix, "actor": None,
            "since": since, "until": until,
        },
        cross_org=True,
    )


@bp.route("/users/force_reset", methods=["POST"])
@require_program_owner
def users_force_reset():
    """Flip ``password_changed_at = NULL`` so the target user is forced
    through ``/login/first-password-set`` on next login.

    Guards:
      * non-numeric ``user_id`` is treated as missing (no-op redirect).
      * a program-owner cannot force-reset another program-owner — that
        would let any compromised owner take over the master account.
      * the action is audit-logged so ops can correlate a forced reset
        with any follow-up logins by the target.
    """
    from flask import flash
    try:
        user_id = int(request.form.get("user_id") or 0)
    except (TypeError, ValueError):
        # Non-numeric form value — treat as missing.
        user_id = 0
    if not user_id:
        flash("No user selected.", "error")
        return redirect(url_for("admin.users_list"))
    from core import db as _db
    target = _db.get_user_by_id(user_id)
    if not target:
        flash("User not found.", "error")
        return redirect(url_for("admin.users_list"))
    actor_id = auth.current_user_id()
    if target.get("program_owner") and target.get("id") != actor_id:
        # Cross-owner force-reset is an escalation vector — a hijacked
        # owner could otherwise lock the master account.
        flash(
            "Cannot force-reset another program-owner; use /recover.",
            "error",
        )
        return redirect(url_for("admin.users_list"))
    with _db._get_conn() as c:
        c.execute(
            "UPDATE users SET password_changed_at=NULL WHERE id=?",
            (user_id,),
        )
        c.commit()
    try:
        from core import audit as _audit
        _audit.write_event(
            action="user.force_password_reset",
            actor_user_id=actor_id,
            target_type="user", target_id=user_id,
            metadata={"target_username": target.get("username")},
            ip=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            ua=request.headers.get("User-Agent", ""),
        )
    except Exception:
        # Audit-log write failed (DB hiccup); don't undo the reset, but
        # surface the drop so ops sees a missing compliance trail.
        import logging
        logging.getLogger(__name__).exception(
            "audit.write_event(user.force_password_reset) failed uid=%s",
            user_id,
        )
    flash(
        f"Forced password reset for {target.get('username')!r}. "
        "They will be required to set a new password on next login.",
        "success",
    )
    return redirect(url_for("admin.users_list"))
