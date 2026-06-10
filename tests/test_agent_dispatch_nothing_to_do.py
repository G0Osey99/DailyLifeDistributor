"""agent_dispatch.start() with every row already uploaded must terminate
the pre-registered SSE job instead of leaving it hanging.

Live-reproduced: a Rock-only re-run of an already-uploaded date (idempotent
skip) returned from start() without sending a job_plan AND without emitting
any frame — the dashboard sat on "dispatching…" forever and the job entry
never became reapable.
"""
from __future__ import annotations

import json

from core import agent_dispatch, db, upload_jobs


def _summary_row(date="2026-06-22", platform="Rock"):
    return {"date": date, "iso_date": date, "platform": platform,
            "title": "t", "skipped": False}


def test_nothing_to_do_emits_skips_and_done(monkeypatch, tmp_path):
    # Record a prior success so filter_done_rows drops the row.
    db.init_db()
    db.record_upload(session_id="S1", iso_date="2026-06-22", platform="Rock",
                     title="t", file_path="", success=True, url="u",
                     scheduled_time="", error="")

    job_id = "job-nothing-1"
    job = upload_jobs.register_job(job_id)
    try:
        sent = []
        monkeypatch.setattr(agent_dispatch._relay, "send_to_device",
                            lambda *a, **k: sent.append(a))

        out = agent_dispatch.start(
            session_id="S1",
            summary=[_summary_row()],
            entries={},
            elements={},
            config={},
            job_id=job_id,
        )

        assert out == job_id
        assert sent == [], "no job_plan should be sent when nothing to do"
        assert job["done"] is True, "job must be terminal so SSE closes"
        assert job["finished_at"] is not None

        frames = []
        while not job["queue"].empty():
            frames.append(json.loads(job["queue"].get_nowait()))
        types = [f["type"] for f in frames]
        assert "skip" in types, f"expected a skip frame, got {types}"
        assert types[-1] == "done"
        skip = next(f for f in frames if f["type"] == "skip")
        assert skip["platform"] == "Rock"
        assert skip["date"] == "2026-06-22"
    finally:
        upload_jobs.drop_job(job_id)


def test_nothing_to_do_without_registered_job_is_safe(monkeypatch):
    """start() must not blow up when the caller didn't pre-register the job
    (defensive: agent_dispatch can be driven by tests/other callers)."""
    db.init_db()
    db.record_upload(session_id="S2", iso_date="2026-06-23", platform="Rock",
                     title="t", file_path="", success=True, url="u",
                     scheduled_time="", error="")
    monkeypatch.setattr(agent_dispatch._relay, "send_to_device",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    out = agent_dispatch.start(
        session_id="S2",
        summary=[_summary_row(date="2026-06-23")],
        entries={}, elements={}, config={}, job_id="job-unregistered",
    )
    assert out == "job-unregistered"
