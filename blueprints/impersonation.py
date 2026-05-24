"""Program-owner impersonation: act as <org> for support/testing.

Sets ``session['acting_as_org_id']``; the rest of the app picks it up
via ``core.org_context.effective_org_id()``. Audit-logged on entry and
exit. Real ``user_id`` never changes.
"""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, request, session, url_for

from core import audit, org_store
from core.permissions import require_program_owner
from core.org_context import real_user_id

bp = Blueprint("impersonation", __name__)


@bp.route("/admin/organizations/<int:org_id>/impersonate", methods=["POST"])
@require_program_owner
def start(org_id: int):
    org = org_store.get_org_by_id(org_id)
    if org is None:
        abort(404)
    session["acting_as_org_id"] = int(org_id)
    audit.write_event(
        action="impersonation.start",
        actor_user_id=real_user_id(),
        org_id=org_id,
        acting_as_org_id=org_id,
        metadata={"org_name": org.get("name")},
        ip=request.remote_addr,
        ua=request.headers.get("User-Agent"),
    )
    flash(f"Now acting as {org.get('name')}. Exit when finished.", "info")
    return redirect(url_for("admin.organization_detail", org_id=org_id))


@bp.route("/admin/exit-impersonation", methods=["POST"])
@require_program_owner
def end():
    prev = session.pop("acting_as_org_id", None)
    if prev is not None:
        audit.write_event(
            action="impersonation.end",
            actor_user_id=real_user_id(),
            org_id=int(prev),
            acting_as_org_id=int(prev),
            ip=request.remote_addr,
            ua=request.headers.get("User-Agent"),
        )
        flash("Impersonation ended.", "info")
    return redirect(request.referrer or url_for("admin.landing"))
