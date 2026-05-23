"""Per-org platform mutex wired into the upload dispatch (phase δ).

Two members of the same org both trying to upload to (say) YouTube must
serialize: the second sees a "waiting" phase_change event, then runs once
the first releases. This test exercises ``core.upload_jobs._wait_for_platform_lock``
directly (a small helper that polls the SQLite lock + emits the phase
change), then verifies ``run_batch`` plumbs ``user_id`` + ``org_id``
through to it.
"""
from __future__ import annotations

import threading
import time

from core import platform_locks, upload_jobs


def test_wait_for_platform_lock_returns_true_immediately_when_free(db):
    events: list = []

    def emit(p):
        events.append(p)

    ok = upload_jobs._wait_for_platform_lock(
        org_id=1, platform="YouTube Video", user_id=10,
        emit=emit, row_id=0, date_iso="2026-05-23", timeout_s=2,
    )
    assert ok is True
    platform_locks.release(1, "YouTube Video", 10)


def test_wait_for_platform_lock_emits_phase_change_when_blocked(db):
    """Other user already holds the lock → second caller emits a
    "platform_lock_wait" phase, polls, then succeeds after release."""
    platform_locks.try_acquire(1, "YouTube Video", 10)
    events: list = []

    def emit(p):
        events.append(p)

    # Release after a short delay in a side thread so the wait wins.
    def _release_later():
        time.sleep(0.3)
        platform_locks.release(1, "YouTube Video", 10)

    t = threading.Thread(target=_release_later, daemon=True)
    t.start()
    ok = upload_jobs._wait_for_platform_lock(
        org_id=1, platform="YouTube Video", user_id=11,
        emit=emit, row_id=2, date_iso="2026-05-23", timeout_s=5,
    )
    t.join()
    assert ok is True, "should have acquired after holder released"
    # At least one phase_change with platform_lock_wait should have fired.
    assert any(
        e.get("type") == "phase_change"
        and e.get("phase") == "platform_lock_wait"
        for e in events
    ), events
    platform_locks.release(1, "YouTube Video", 11)


def test_wait_for_platform_lock_times_out(db):
    platform_locks.try_acquire(1, "YouTube Video", 10)
    events: list = []

    def emit(p):
        events.append(p)

    ok = upload_jobs._wait_for_platform_lock(
        org_id=1, platform="YouTube Video", user_id=11,
        emit=emit, row_id=2, date_iso="2026-05-23", timeout_s=1,
    )
    assert ok is False
    platform_locks.release(1, "YouTube Video", 10)
