"""Phase γ Task 30: nightly archive_old_entries(>365d)."""
from __future__ import annotations

from freezegun import freeze_time

from core import audit, audit_archive


def test_old_rows_moved_in_batches(db):
    with freeze_time("2025-01-01"):
        for i in range(2500):
            audit.write_event(action="upload.started", actor_user_id=i, org_id=1)
    with freeze_time("2026-05-23"):
        audit.write_event(action="user.login", actor_user_id=42, org_id=1)
        n = audit_archive.archive_old_entries(batch_size=1000)
    assert n == 2500
    active = db.list_audit_events(limit=10000)
    assert len(active) == 1
    assert active[0]["action"] == "user.login"
    archived = db.list_audit_archive(limit=10000)
    assert len(archived) == 2500


def test_archive_is_idempotent_when_no_old_rows(db):
    audit.write_event(action="user.login", actor_user_id=1, org_id=1)
    n = audit_archive.archive_old_entries()
    assert n == 0
