"""Per-user RunLock — two users may run web uploads concurrently.

Phase δ lifts the web RunLock from process-global to per-user. Same user can
only hold one run at a time; different users do not block each other.
"""
from core import media_session as ms


def test_per_user_lock_same_user_blocks():
    lock = ms.PerUserRunLock()
    assert lock.acquire(user_id=1, run_id="run-a") is True
    # Same user, different run → refused (they already hold a run).
    assert lock.acquire(user_id=1, run_id="run-b") is False
    lock.release(user_id=1, run_id="run-a")
    # Released → free for the same user to start a new one.
    assert lock.acquire(user_id=1, run_id="run-b") is True
    lock.release(user_id=1, run_id="run-b")


def test_per_user_lock_different_users_independent():
    lock = ms.PerUserRunLock()
    assert lock.acquire(user_id=1, run_id="run-1") is True
    # Different user → independent slot.
    assert lock.acquire(user_id=2, run_id="run-2") is True
    assert lock.holder(user_id=1) == "run-1"
    assert lock.holder(user_id=2) == "run-2"
    lock.release(user_id=1, run_id="run-1")
    assert lock.holder(user_id=1) is None
    assert lock.holder(user_id=2) == "run-2"
    lock.release(user_id=2, run_id="run-2")


def test_per_user_release_wrong_run_id_is_noop():
    lock = ms.PerUserRunLock()
    lock.acquire(user_id=1, run_id="run-a")
    # Releasing a non-holder run_id is a no-op (mirrors RunLock behavior).
    lock.release(user_id=1, run_id="run-other")
    assert lock.holder(user_id=1) == "run-a"
    lock.release(user_id=1, run_id="run-a")
    assert lock.holder(user_id=1) is None


def test_per_user_lock_holder_lookup_returns_none_for_unknown_user():
    lock = ms.PerUserRunLock()
    assert lock.holder(user_id=999) is None


def test_per_user_lock_find_by_run_id():
    """Releasing on /media/run/finish only knows the run_id, so the lock
    must support a reverse lookup."""
    lock = ms.PerUserRunLock()
    lock.acquire(user_id=42, run_id="run-x")
    assert lock.user_for_run("run-x") == 42
    assert lock.user_for_run("run-nope") is None
    lock.release(user_id=42, run_id="run-x")
    assert lock.user_for_run("run-x") is None
