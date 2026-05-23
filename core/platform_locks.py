"""Per-org per-platform soft mutex used by the web upload dispatch.

If two members of the same org both try to upload to (say) YouTube at the
same time, the second waits on this lock instead of trampling the first's
upload session. The lock has a 30-minute default TTL so a crash or stuck
worker can't wedge the org's platform forever.

Backed by the SQLite ``platform_locks`` table (added in phase δ); the
table's PRIMARY KEY (org_id, platform) gives us a single holder per pair.
Re-acquiring with the same (org_id, platform, user_id) is idempotent —
extends the lease rather than failing — so the dispatcher's per-row retry
loop doesn't deadlock against itself.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.db import _get_conn

_DEFAULT_TTL_SECONDS = 30 * 60  # 30 minutes


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def try_acquire(
    org_id: int,
    platform: str,
    user_id: int,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> bool:
    """Atomically take the (org_id, platform) lock for user_id.

    Returns True if the lock was acquired (or re-acquired by the same
    user, extending the lease). Returns False if a different user is
    currently holding a non-expired lease.

    Expired locks are auto-released on each call: the implementation
    DELETEs any row whose ``expires_at`` is in the past before the INSERT
    attempt, so a stuck worker can't block the next caller past its TTL.
    """
    now = _now()
    expires = now + timedelta(seconds=max(1, int(ttl_seconds)))
    with _get_conn() as c:
        # 1. Clear any expired lease for this (org, platform).
        c.execute(
            "DELETE FROM platform_locks "
            "WHERE org_id = ? AND platform = ? AND expires_at <= ?",
            (org_id, platform, _iso(now)),
        )
        # 2. Look at the current holder (if any).
        row = c.execute(
            "SELECT locked_by_user_id FROM platform_locks "
            "WHERE org_id = ? AND platform = ?",
            (org_id, platform),
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO platform_locks "
                "(org_id, platform, locked_by_user_id, locked_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (org_id, platform, user_id, _iso(now), _iso(expires)),
            )
            c.commit()
            return True
        if int(row["locked_by_user_id"]) == int(user_id):
            # Same user re-acquiring: extend the lease.
            c.execute(
                "UPDATE platform_locks SET locked_at = ?, expires_at = ? "
                "WHERE org_id = ? AND platform = ?",
                (_iso(now), _iso(expires), org_id, platform),
            )
            c.commit()
            return True
        return False


def release(org_id: int, platform: str, user_id: int) -> None:
    """Release the lock iff the caller is the current holder.

    A stale release from a different user is a no-op (mirrors RunLock).
    """
    with _get_conn() as c:
        c.execute(
            "DELETE FROM platform_locks "
            "WHERE org_id = ? AND platform = ? AND locked_by_user_id = ?",
            (org_id, platform, user_id),
        )
        c.commit()


def current_holder(org_id: int, platform: str) -> dict | None:
    """Return the current row (as a dict) or None if no lock is held."""
    with _get_conn() as c:
        row = c.execute(
            "SELECT org_id, platform, locked_by_user_id, locked_at, expires_at "
            "FROM platform_locks WHERE org_id = ? AND platform = ?",
            (org_id, platform),
        ).fetchone()
    return dict(row) if row else None
