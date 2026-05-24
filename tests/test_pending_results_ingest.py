"""Tests for apply_pending_results — idempotent server-side ingest + ack."""


def test_pending_results_idempotent_application():
    """Applying the same entry twice records exactly one row in upload_history."""
    from core import agent_dispatch, db as _db

    # Register job so apply_pending_results can find the session_id.
    import queue
    agent_dispatch.register_job(job_id="J9", sse_queue=queue.Queue(), session_id="S9")

    entries = [
        {"job_id": "J9", "row_idx": 0, "iso_date": "2026-05-22",
         "platform": "YouTube Video", "status": "success",
         "payload": {"watch_url": "u"}},
    ]

    # First apply: records the row.
    acked1 = agent_dispatch.apply_pending_results(entries)
    assert _db.has_successful_upload("S9", "2026-05-22", "YouTube Video") is True
    assert acked1 == [("J9", 0, "YouTube Video")]

    # Second apply: idempotent — no duplicate row, still returns ack key.
    acked2 = agent_dispatch.apply_pending_results(entries)
    assert acked2 == [("J9", 0, "YouTube Video")]

    # Exactly one success row in history for this (date, platform).
    rows = _db.get_history(session_id="S9")
    matching = [
        r for r in rows
        if r["iso_date"] == "2026-05-22"
        and r["platform"] == "YouTube Video"
        and r["success"] == 1
    ]
    assert len(matching) == 1


def test_apply_pending_results_unknown_job_still_acks():
    """An entry whose job_id isn't registered is acked without error
    (server may have already dropped the job from its registry)."""
    from core import agent_dispatch

    entries = [
        {"job_id": "UNKNOWN", "row_idx": 0, "iso_date": "2026-05-22",
         "platform": "Rock", "status": "success", "payload": {}},
    ]
    # Should not raise; acked list includes the key (best-effort ack).
    acked = agent_dispatch.apply_pending_results(entries)
    assert acked == [("UNKNOWN", 0, "Rock")]


def test_apply_pending_results_multiple_entries():
    """Multiple entries are each processed; all are acked."""
    from core import agent_dispatch, db as _db
    import queue

    agent_dispatch.register_job(job_id="JM", sse_queue=queue.Queue(), session_id="SM")
    entries = [
        {"job_id": "JM", "row_idx": 0, "iso_date": "2026-05-22",
         "platform": "YouTube Video", "status": "success", "payload": {}},
        {"job_id": "JM", "row_idx": 0, "iso_date": "2026-05-22",
         "platform": "Rock", "status": "success", "payload": {}},
        {"job_id": "JM", "row_idx": 1, "iso_date": "2026-05-23",
         "platform": "YouTube Video", "status": "success", "payload": {}},
    ]
    acked = agent_dispatch.apply_pending_results(entries)
    assert len(acked) == 3
    assert _db.has_successful_upload("SM", "2026-05-22", "YouTube Video") is True
    assert _db.has_successful_upload("SM", "2026-05-22", "Rock") is True
    assert _db.has_successful_upload("SM", "2026-05-23", "YouTube Video") is True
