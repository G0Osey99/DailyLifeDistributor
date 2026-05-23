"""Phase γ Task 31: APScheduler is registered with the nightly archive job."""
from __future__ import annotations


def test_scheduler_has_audit_archive_job(app):
    sched = app.config.get("scheduler")
    assert sched is not None, "APScheduler not installed on app.config"
    job_ids = {j.id for j in sched.get_jobs()}
    assert "audit_archive" in job_ids


def test_scheduler_not_started_under_testing(app):
    """Under TESTING the scheduler exists but is paused (not running)."""
    sched = app.config.get("scheduler")
    assert sched is not None
    # The point is that we never start it in tests — it would emit
    # warnings on every test teardown otherwise.
    assert not sched.running
