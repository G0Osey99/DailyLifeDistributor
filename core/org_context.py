"""Effective-org resolver for credential reads + impersonation helpers.

Two session keys cooperate:

* ``current_org_id`` — the user's actively-selected membership org.
  Populated at login (auto for single-membership users, by the picker
  for multi-membership users). Permission decorators use this for role
  checks: a role check must always look at the *real* membership.

* ``acting_as_org_id`` — optional; set only by the program-owner
  impersonation route. While present, ``effective_org_id()`` returns
  this instead of ``current_org_id``. Real ``user_id`` never changes.

Credential reads MUST go through ``effective_org_id()``. Role checks
MUST keep reading ``session['current_org_id']`` directly so an owner
acting as another org cannot grant themselves a role they don't have.
"""
from __future__ import annotations

from functools import wraps
from typing import Optional

from flask import abort, session


def real_user_id() -> Optional[int]:
    """The authenticated user's id. Never affected by impersonation."""
    uid = session.get("user_id")
    return int(uid) if uid is not None else None


def current_org_id() -> Optional[int]:
    """The user's selected membership org. Real, not impersonated."""
    oid = session.get("current_org_id")
    return int(oid) if oid is not None else None


def acting_as_org_id() -> Optional[int]:
    """The org the program owner is impersonating, or None."""
    oid = session.get("acting_as_org_id")
    return int(oid) if oid is not None else None


def is_impersonating() -> bool:
    return acting_as_org_id() is not None


def effective_org_id() -> Optional[int]:
    """Org used for credential reads and audit org_id fill-ins.

    Returns acting_as_org_id when set, else current_org_id, else None.
    None means 'no session / no membership' — callers must treat that
    as a hard miss (don't fall back to legacy unscoped).
    """
    return acting_as_org_id() or current_org_id()


def forbidden_during_impersonation(view):
    """409 the request when ``acting_as_org_id`` is set.

    Applied to routes that change account security: 2FA setup/disable,
    password change, recovery approval, membership role changes. The
    program owner must exit impersonation before they're allowed to
    touch those endpoints on behalf of another tenant.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_impersonating():
            abort(409, description=(
                "This action is not allowed while acting as another "
                "organization. Exit impersonation and try again."
            ))
        return view(*args, **kwargs)
    return wrapped
