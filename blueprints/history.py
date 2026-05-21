"""Past-sessions history page + resume-session action."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, url_for

from core import db as _db
from core.session_state import SessionState, session

bp = Blueprint("history", __name__)


@bp.route("/history")
def history():
    """Show past upload sessions and their records."""
    sessions = _db.list_sessions(limit=50)

    for s in sessions:
        records = _db.get_history(session_id=s["id"], limit=1000)
        s["total_uploads"] = len(records)
        s["total_success"] = sum(1 for r in records if r.get("success"))
        s["records"] = records

    return render_template("history.html", sessions=sessions)


@bp.route("/resume-session", methods=["POST"])
def resume_session():
    """Mutate the singleton SessionState in place from the latest DB row."""
    loaded = SessionState.resume_latest()
    if loaded is None:
        flash("No in-progress session found.", "warning")
        return redirect(url_for("scan.index"))

    session.replace_with(loaded)
    flash(f"Resumed session: {', '.join(session.selected_dates)}", "success")
    return redirect(url_for("review.review"))
