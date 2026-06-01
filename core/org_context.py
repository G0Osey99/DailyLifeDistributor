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

import threading
from contextlib import contextmanager
from functools import wraps
from typing import Iterator, Optional

try:
    from flask import abort, has_request_context, session
except ModuleNotFoundError:  # pragma: no cover - exercised only in the agent bundle
    # The bundled hybrid agent ships no Flask, but it still imports this module:
    # the uploaders call effective_org_id() to scope credential reads. Off the
    # server there is never a request context, so the helpers below degrade to
    # None — and the agent's secrets shim ignores org_id anyway, returning the
    # envelope's credentials. ARCH-007: a core module must not hard-require
    # Flask just to be imported.
    def has_request_context() -> bool:  # type: ignore
        return False

    session = None  # type: ignore

    def abort(*_a, **_k):  # type: ignore
        # Only reachable via forbidden_during_impersonation, a route decorator
        # that never runs off-server. Fail loud if it somehow does.
        raise RuntimeError("flask.abort() called but Flask is not installed")


# Thread-local org override. Used by worker threads that ran originally
# inside a Flask request context but now execute outside it — e.g. the
# calendar refresh's ThreadPoolExecutor workers and the /upload web
# worker (``blueprints/media._run_batch_worker``). Without this override,
# ``effective_org_id()`` returns None in those threads, credential reads
# fall through to the (empty post-migration) legacy unscoped slot, and
# the refresh / upload fails with "session expired" even though the
# sidebar's request-context-aware status shows everything green.
#
# Set at the worker entry point with ``with org_context.override(oid):``
# and the rest of the call stack inside that thread sees ``oid`` as the
# effective org. Threading-local rather than contextvars so it works
# with vanilla ``threading.Thread.start()`` (no asyncio / no automatic
# context copy needed).
_local = threading.local()


@contextmanager
def override(org_id: int | None) -> Iterator[None]:
    """Within the ``with`` block, ``effective_org_id()`` returns *org_id*.

    Use at the entry point of a background worker that needs to honor the
    org context the request set up. A ``None`` value enters the block
    transparently (no-op); useful for callers that want to write
    ``with override(maybe_oid):`` without branching.
    """
    if org_id is None:
        # Don't shadow any existing thread-local value with None.
        yield
        return
    prev = getattr(_local, "org_id", None)
    _local.org_id = int(org_id)
    try:
        yield
    finally:
        if prev is None:
            try:
                delattr(_local, "org_id")
            except AttributeError:
                pass
        else:
            _local.org_id = prev


def _thread_override() -> int | None:
    return getattr(_local, "org_id", None)


def real_user_id() -> Optional[int]:
    """The authenticated user's id. Never affected by impersonation.

    Returns None when called outside a Flask request context (e.g. the
    hybrid agent path or background threads with no app context pushed).
    """
    if not has_request_context():
        return None
    uid = session.get("user_id")
    return int(uid) if uid is not None else None


def current_org_id() -> Optional[int]:
    """The user's selected membership org. Real, not impersonated.

    Mirrors ``core.auth.current_org_id()``; the duplicate is intentional
    so callers can import both real and effective org from one module.
    Returns None outside a Flask request context.
    """
    if not has_request_context():
        return None
    oid = session.get("current_org_id")
    return int(oid) if oid is not None else None


def acting_as_org_id() -> Optional[int]:
    """The org the program owner is impersonating, or None."""
    if not has_request_context():
        return None
    oid = session.get("acting_as_org_id")
    return int(oid) if oid is not None else None


def is_impersonating() -> bool:
    """True when the program owner has set an acting_as_org_id in session."""
    return acting_as_org_id() is not None


def effective_org_id() -> Optional[int]:
    """Org used for credential reads and audit org_id fill-ins.

    Resolution order:
      1. Thread-local override set by ``override(org_id)`` (worker
         threads that captured the org from a request context).
      2. ``acting_as_org_id`` from the Flask session (impersonation).
      3. ``current_org_id`` from the Flask session (real membership).
      4. None — no session / no membership. Callers must treat None as
         a hard miss, never fall back to the legacy unscoped slot.
    """
    o = _thread_override()
    if o is not None:
        return o
    acting = acting_as_org_id()
    if acting is not None:
        return acting
    return current_org_id()


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
