"""Past-sessions history page.

The legacy server-side "resume session" action was removed with the old
review/upload flow: in the browser-streaming model the media lives on the
user's machine, so a run can't be resumed server-side — recovery is to
re-select the remaining dates (the idempotent skip prevents duplicates).
"""
from __future__ import annotations

from flask import Blueprint, render_template

from core import db as _db

bp = Blueprint("history", __name__)


@bp.route("/history")
def history():
    """Show past upload sessions and their records, scoped to the active org."""
    from core.org_context import effective_org_id
    org_id = effective_org_id()
    sessions = _db.list_sessions(limit=50, org_id=org_id)

    for s in sessions:
        records = _db.get_history(session_id=s["id"], limit=1000, org_id=org_id)
        s["total_uploads"] = len(records)
        s["total_success"] = sum(1 for r in records if r.get("success"))
        s["records"] = records

    return render_template("history.html", sessions=sessions)
