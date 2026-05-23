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
    org_store.create_org(
        name=name, slug=slug,
        created_by_user_id=auth.current_user_id(),
    )
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
