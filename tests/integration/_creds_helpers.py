"""Shared helpers for ``*_live.py`` skipif gates.

The ``_live.py`` integration tests opt out of the autouse state.db isolation
fixture (see ``tests/integration/conftest.py``) so they can read the real
encrypted secrets the operator paired. That means the skipif expression at
the top of each live test runs against whatever DB is on disk — which on CI
and on fresh dev installs is either missing entirely or unmigrated (no
``secrets`` table). Any exception from ``secrets_store`` at collection time
turns a collection-time skip into a hard collection error and the entire
test session fails before a single test runs.

This module wraps the secrets-store calls so the gates can never throw —
any failure (missing DB, missing table, missing/invalid master key,
``MasterKeyError``, ``sqlite3.OperationalError``, decryption errors, etc.)
collapses to ``False`` so the live test simply skips.
"""
from __future__ import annotations


def safely_has_credential(*names: str) -> bool:
    """Return True only if every ``name`` has a readable secret/blob.

    Returns False on CI / fresh installs where the DB is unmigrated, where
    the master key is missing, or where decryption fails for any reason.
    Catches a bare ``Exception`` on purpose — we only care that the gate is
    safe to evaluate at collection time, not why it failed.
    """
    if not names:
        return False
    try:
        from core import secrets_store
    except Exception:
        return False
    for name in names:
        try:
            if not secrets_store.has_secret(name):
                return False
        except Exception:
            return False
        # Confirm the value is actually decryptable. ``has_secret`` only
        # checks row presence; ``get_secret``/``get_blob`` exercises the
        # Fernet decrypt path so a wrong/missing master key skips cleanly.
        try:
            if (
                secrets_store.get_secret(name) is None
                and secrets_store.get_blob(name) is None
            ):
                return False
        except Exception:
            return False
    return True
