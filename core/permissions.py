"""Authorization decorators.

Three guards:
  * `@require_program_owner` — strictest; gates /admin/* on `users.program_owner`.
  * `@require_role(*roles)`  — gates per-org routes on the session's role in
    `current_org_id`. Program-owners bypass the role check (they manage all orgs).
  * `@require_authenticated_json` — for JSON endpoints (e.g. agent pair-redeem)
    that need a session but no specific role; returns 401 instead of redirecting.

Legacy mode (`LEGACY_PASSWORD_ENABLED=true`) lets the shared-password session
through `require_role` automatically — the existing test suite + the
single-tenant USB install both rely on that path. Program-owner bypass still
applies on top.
"""
from __future__ import annotations

from functools import wraps
from typing import Optional

from flask import abort, redirect, request, session, url_for

from core import auth, db, user_store

_VALID_ROLES = ("owner", "manager", "user")


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


def _lookup_role(user_id: int, org_id: int) -> Optional[str]:
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT role FROM org_memberships "
            "WHERE user_id = ? AND org_id = ?",
            (user_id, org_id),
        ).fetchone()
    return row["role"] if row else None


def is_program_owner(user_id: int) -> bool:
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT program_owner FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    return bool(row and row["program_owner"])


# Re-export of core.auth.legacy_enabled so the role decorators below
# can call _legacy_mode() unchanged without re-implementing the parse.
from core.auth import legacy_enabled as _legacy_mode


def require_role(*roles: str):
    """Role-based access control decorator.

    Anonymous → redirect to /login (or 401 JSON).
    Program-owner → bypass (manages all orgs).
    Legacy shared-password mode → bypass (single-tenant compatibility).
    No current_org_id → 403.
    Role not in *roles* → 403.
    """
    for r in roles:
        if r not in _VALID_ROLES:
            raise ValueError(f"unknown role: {r}")

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user_id = session.get("user_id")
            # Legacy shared-password session: has "authenticated" but no
            # user_id. Treat as full-access (single-tenant USB install).
            if _legacy_mode() and session.get("authenticated") and not user_id:
                return fn(*args, **kwargs)
            if not user_id:
                wants_json = (
                    request.headers.get("X-Requested-With") == "XMLHttpRequest"
                    or "application/json" in request.headers.get("Accept", "")
                    or request.is_json
                )
                if wants_json:
                    return ("", 401)
                return redirect(url_for("auth.login", next=request.path))
            if is_program_owner(user_id):
                return fn(*args, **kwargs)
            org_id = session.get("current_org_id")
            if not org_id:
                abort(403)
            role = _lookup_role(user_id, org_id)
            if role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def require_authenticated_json(fn):
    """Lightweight 401-on-anonymous guard for JSON endpoints.

    Used by the agent pair-redeem POST, which needs a logged-in member
    (any role) but isn't a browser navigation that should redirect.
    Legacy shared-password sessions pass through.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _legacy_mode() and session.get("authenticated"):
            return fn(*args, **kwargs)
        if not session.get("user_id"):
            return ("", 401)
        return fn(*args, **kwargs)
    return wrapper
