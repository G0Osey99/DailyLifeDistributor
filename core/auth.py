"""Single shared-credential authentication.

The password hash (Werkzeug scrypt) is stored in the encrypted secret store
under `auth.password_hash`. First-boot bootstrap seeds it from the
INITIAL_ADMIN_PASSWORD env var so an open VPS has no public "set password"
page to race. A small in-process per-IP lockout slows brute force.
"""
from __future__ import annotations

import logging
import os
import time

from werkzeug.security import check_password_hash, generate_password_hash

from core import secrets_store

log = logging.getLogger(__name__)

_HASH_SECRET = "auth.password_hash"
_INITIAL_ENV = "INITIAL_ADMIN_PASSWORD"

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

# ip -> (failure_count, first_failure_monotonic)
_failures: dict[str, tuple[int, float]] = {}


def is_configured() -> bool:
    return secrets_store.has_secret(_HASH_SECRET)


def set_password(password: str) -> None:
    secrets_store.set_secret(_HASH_SECRET, generate_password_hash(password))


def verify_password(password: str) -> bool:
    """Return True if `password` matches the stored credential.

    Returns False when no credential is configured yet (use is_configured()
    to distinguish a fresh, unconfigured deploy from a wrong password).
    """
    stored = secrets_store.get_secret(_HASH_SECRET)
    if not stored:
        return False
    return check_password_hash(stored, password)


def bootstrap_from_env() -> None:
    """Seed the credential from INITIAL_ADMIN_PASSWORD if none is set yet."""
    if is_configured():
        return
    seed = (os.environ.get(_INITIAL_ENV) or "").strip()
    if not seed:
        log.warning(
            "No login credential is set and %s is not provided. Set %s and "
            "restart to create the initial login.", _INITIAL_ENV, _INITIAL_ENV,
        )
        return
    if len(seed) < 8:
        log.warning(
            "%s is shorter than 8 characters; use a stronger initial password.",
            _INITIAL_ENV,
        )
    set_password(seed)
    log.info("Seeded initial login credential from %s.", _INITIAL_ENV)


def record_failure(ip: str) -> None:
    count, first = _failures.get(ip, (0, time.monotonic()))
    _failures[ip] = (count + 1, first)


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)


def is_locked(ip: str) -> bool:
    entry = _failures.get(ip)
    if entry is None:
        return False
    count, first = entry
    if time.monotonic() - first > LOCKOUT_SECONDS:
        # Window elapsed: don't fully reset, or an attacker could get
        # MAX_ATTEMPTS-1 free guesses every window forever. Keep the count
        # one below the threshold so the next failure immediately re-locks.
        _failures[ip] = (MAX_ATTEMPTS - 1, time.monotonic())
        return False
    return count >= MAX_ATTEMPTS


def reset_lockouts() -> None:
    """Test helper: clear all tracked failures."""
    _failures.clear()


# ---- Multi-tenant phase α: session-shape helpers ----
#
# Sessions are keyed by user_id (and optionally current_org_id). The legacy
# boolean `authenticated` is honored ONLY when LEGACY_PASSWORD_ENABLED is
# set — gives ops one release to roll back if Argon2id login breaks.

from flask import session as _flask_session


def legacy_enabled() -> bool:
    """True iff the operator opted the deploy into the pre-multi-tenant
    shared-password login. Canonical helper for the project — every other
    parse site (blueprints/auth.py, core/permissions.py) should import
    this, not re-implement the env parsing."""
    return (os.environ.get("LEGACY_PASSWORD_ENABLED", "") or "").lower() in (
        "1", "true", "yes",
    )


# Back-compat alias — kept so any external import that still uses the
# underscored name doesn't break. Remove in a follow-up.
_legacy_enabled = legacy_enabled


def is_authenticated() -> bool:
    if _flask_session.get("user_id") is not None:
        return True
    if legacy_enabled() and bool(_flask_session.get("authenticated")):
        return True
    return False


def current_user_id() -> int | None:
    uid = _flask_session.get("user_id")
    return int(uid) if uid is not None else None


def current_org_id() -> int | None:
    oid = _flask_session.get("current_org_id")
    return int(oid) if oid is not None else None
