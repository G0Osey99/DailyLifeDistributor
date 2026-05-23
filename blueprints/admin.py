"""Program-owner admin (multi-tenant phase α).

Routes here are gated by users.program_owner = TRUE. The org-create form
is bare-bones in α — invite-on-create lands in PR-β. The user-list page
has a force-password-reset action that writes nothing yet (placeholder;
email sending wires up in PR-β).
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
    orgs = org_store.list_orgs()
    return render_template(
        "admin/organizations.html",
        orgs=orgs,
        form_error=None,
        landing=True,
    )


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
        pass
    return redirect(url_for("admin.organizations_list"))


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
    """Placeholder: PR-β wires the actual Resend send. For now it just
    flips password_changed_at to NULL so the next login is blocked until
    the user calls /reset-password (also PR-β)."""
    user_id = int(request.form.get("user_id") or 0)
    if not user_id:
        return redirect(url_for("admin.users_list"))
    from core import db as _db
    with _db._get_conn() as c:
        c.execute(
            "UPDATE users SET password_changed_at=NULL WHERE id=?",
            (user_id,),
        )
        c.commit()
    return redirect(url_for("admin.users_list"))
