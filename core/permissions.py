"""Authorization decorators."""
from __future__ import annotations

from functools import wraps
from flask import abort, redirect, request, url_for

from core import auth, user_store


def require_program_owner(view):
    """403 unless the session's user is flagged users.program_owner=TRUE.

    Anonymous callers redirect to login (so a clipped URL is recoverable);
    authenticated non-owners get a hard 403 (no leaking which routes exist).
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        uid = auth.current_user_id()
        if uid is None:
            return redirect(url_for("auth.login", next=request.path))
        user = user_store.get_user_by_id(uid)
        if not user or not user.get("program_owner"):
            abort(403)
        return view(*args, **kwargs)
    return wrapped
