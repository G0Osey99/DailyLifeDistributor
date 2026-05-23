"""Move audit_log rows older than 365 days into audit_log_archive.

Batched so a one-time backfill on a deploy with millions of rows doesn't
hold the SQLite write lock for an unbounded amount of time. Idempotent:
re-running with no old rows is a single indexed `SELECT ... LIMIT` and
returns 0.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import db as _db

_RETENTION = timedelta(days=365)


def archive_old_entries(batch_size: int = 1000) -> int:
    """Move every row older than 365 days into audit_log_archive.

    Returns the total number of rows moved across all batches.
    """
    cutoff = (datetime.now(timezone.utc) - _RETENTION).isoformat()
    total = 0
    while True:
        moved = _db.archive_audit_batch(cutoff, batch_size)
        if moved == 0:
            break
        total += moved
    return total
