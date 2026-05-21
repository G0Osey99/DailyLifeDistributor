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
        _failures.pop(ip, None)  # window elapsed; reset
        return False
    return count >= MAX_ATTEMPTS


def reset_lockouts() -> None:
    """Test helper: clear all tracked failures."""
    _failures.clear()
