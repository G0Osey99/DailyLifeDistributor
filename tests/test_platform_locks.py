"""Per-org platform mutex helpers (core.platform_locks).

try_acquire / release / current_holder, with stale lock auto-release via
the row's expires_at.
"""
from __future__ import annotations

import time

from core import platform_locks


def test_try_acquire_returns_true_for_first_caller(db):
    ok = platform_locks.try_acquire(org_id=1, platform="youtube", user_id=10)
    assert ok is True
    holder = platform_locks.current_holder(1, "youtube")
    assert holder is not None
    assert holder["locked_by_user_id"] == 10
    platform_locks.release(1, "youtube", 10)


def test_try_acquire_returns_false_when_held(db):
    platform_locks.try_acquire(org_id=1, platform="youtube", user_id=10)
    ok = platform_locks.try_acquire(org_id=1, platform="youtube", user_id=11)
    assert ok is False
    platform_locks.release(1, "youtube", 10)


def test_release_clears_holder(db):
    platform_locks.try_acquire(org_id=1, platform="youtube", user_id=10)
    platform_locks.release(1, "youtube", 10)
    assert platform_locks.current_holder(1, "youtube") is None


def test_release_by_non_holder_is_noop(db):
    platform_locks.try_acquire(org_id=1, platform="youtube", user_id=10)
    # A foreign user_id calling release must not take the lock from the
    # actual holder.
    platform_locks.release(1, "youtube", 999)
    assert platform_locks.current_holder(1, "youtube") is not None
    platform_locks.release(1, "youtube", 10)


def test_different_orgs_independent(db):
    assert platform_locks.try_acquire(1, "youtube", 10) is True
    assert platform_locks.try_acquire(2, "youtube", 11) is True
    platform_locks.release(1, "youtube", 10)
    platform_locks.release(2, "youtube", 11)


def test_different_platforms_independent(db):
    assert platform_locks.try_acquire(1, "youtube", 10) is True
    assert platform_locks.try_acquire(1, "rock", 10) is True
    platform_locks.release(1, "youtube", 10)
    platform_locks.release(1, "rock", 10)


def test_expired_lock_is_auto_released(db, monkeypatch):
    # Acquire with a tiny TTL → wait past it → next try_acquire wins.
    platform_locks.try_acquire(1, "youtube", 10, ttl_seconds=1)
    # Manually expire by rewriting the row's expires_at to the past.
    from core.db import _get_conn
    with _get_conn() as c:
        c.execute(
            "UPDATE platform_locks SET expires_at = '1970-01-01T00:00:00+00:00' "
            "WHERE org_id = 1 AND platform = 'youtube'"
        )
        c.commit()
    ok = platform_locks.try_acquire(1, "youtube", 11)
    assert ok is True, "expired lock should be auto-released"
    platform_locks.release(1, "youtube", 11)


def test_same_user_can_reacquire_held_lock(db):
    """Re-entrant: the same (user_id) re-acquiring extends the lease,
    not a deadlock. Useful for the dispatcher's per-row retry loop."""
    assert platform_locks.try_acquire(1, "youtube", 10) is True
    assert platform_locks.try_acquire(1, "youtube", 10) is True
    platform_locks.release(1, "youtube", 10)
