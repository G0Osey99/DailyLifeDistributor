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

    # PERF-002: one IN-query for all shown sessions, grouped in Python, instead
    # of a per-session get_history (the old N+1 = up to 51 separate-connection
    # full-table scans to render one page).
    records_by_session: dict = {}
    session_ids = [s["id"] for s in sessions]
    # Budget = the old per-session limit (1000) × #sessions shown, so the
    # single IN-query preserves the previous per-session row ceiling in
    # aggregate (the old loop gave each session its own 1000). Bounded: at most
    # 50 sessions → ≤50k rows, one query.
    all_records = _db.get_history_for_sessions(
        session_ids, org_id=org_id, limit=max(5000, len(session_ids) * 1000))
    for r in all_records:
        records_by_session.setdefault(r.get("session_id"), []).append(r)

    for s in sessions:
        records = records_by_session.get(s["id"], [])
        s["total_uploads"] = len(records)
        s["total_success"] = sum(1 for r in records if r.get("success"))
        s["records"] = records

    return render_template("history.html", sessions=sessions)
