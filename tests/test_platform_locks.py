"""Per-org platform mutex helpers (core.platform_locks).

try_acquire / release / current_holder, with stale lock auto-release via
the row's expires_at.
"""
from __future__ import annotations


from core import platform_locks


def test_concurrent_try_acquire_has_exactly_one_winner(db):
    """Regression (CONC-001): the per-org platform mutex must admit exactly
    ONE winner under concurrent acquisition. The old DELETE->SELECT->INSERT
    had a TOCTOU: two callers could both read "no holder" and both INSERT,
    so two users would both proceed to upload (and the loser's INSERT hit an
    IntegrityError that bubbled up). A threading.Barrier maximizes the
    interleaving; with the old code this would surface a second winner or a
    raised exception."""
    import threading

    N = 12
    barrier = threading.Barrier(N)
    results: dict[int, object] = {}
    rlock = threading.Lock()

    def worker(uid: int):
        try:
            barrier.wait(timeout=10)
            ok = platform_locks.try_acquire(org_id=1, platform="youtube", user_id=uid)
        except Exception as e:  # noqa: BLE001 — record, don't let it vanish
            ok = e
        with rlock:
            results[uid] = ok

    threads = [threading.Thread(target=worker, args=(100 + i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)

    errors = [r for r in results.values() if isinstance(r, Exception)]
    assert not errors, f"try_acquire raised under contention: {errors!r}"
    winners = [uid for uid, ok in results.items() if ok is True]
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    holder = platform_locks.current_holder(1, "youtube")
    assert holder is not None and holder["locked_by_user_id"] == winners[0]
    platform_locks.release(1, "youtube", winners[0])


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
