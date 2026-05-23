"""Tests for PendingResults — accumulate success rows for hello-frame replay."""
from agent.dispatch import PendingResults


def test_completed_success_event_is_recorded():
    pr = PendingResults()
    pr.observe({"type": "event", "event": "success", "job_id": "J1", "row_idx": 0,
                "iso_date": "2026-05-22", "platform": "YouTube Video", "payload": {}})
    assert pr.snapshot() == [
        {"job_id": "J1", "row_idx": 0, "iso_date": "2026-05-22",
         "platform": "YouTube Video", "status": "success", "payload": {}}
    ]


def test_non_success_event_ignored():
    pr = PendingResults()
    pr.observe({"type": "event", "event": "start", "job_id": "J1", "row_idx": 0,
                "iso_date": "2026-05-22", "platform": "YouTube Video"})
    assert pr.snapshot() == []


def test_non_event_type_ignored():
    pr = PendingResults()
    pr.observe({"type": "credentials_updated", "key": "k", "value": "v"})
    assert pr.snapshot() == []


def test_dedup_by_job_row_platform():
    pr = PendingResults()
    pr.observe({"type": "event", "event": "success", "job_id": "J1", "row_idx": 0,
                "iso_date": "d", "platform": "P", "payload": {"a": 1}})
    pr.observe({"type": "event", "event": "success", "job_id": "J1", "row_idx": 0,
                "iso_date": "d", "platform": "P", "payload": {"a": 2}})
    snap = pr.snapshot()
    assert len(snap) == 1
    assert snap[0]["payload"] == {"a": 2}  # last write wins


def test_clear_on_ack_removes_acked_keys_only():
    pr = PendingResults()
    pr.observe({"type": "event", "event": "success", "job_id": "J1", "row_idx": 0,
                "iso_date": "d", "platform": "P", "payload": {}})
    pr.observe({"type": "event", "event": "success", "job_id": "J1", "row_idx": 1,
                "iso_date": "d", "platform": "P", "payload": {}})
    pr.clear_acked([("J1", 0, "P")])
    assert [e["row_idx"] for e in pr.snapshot()] == [1]


def test_clear_acked_accepts_lists():
    """clear_acked must work with lists (JSON-decoded) as well as tuples."""
    pr = PendingResults()
    pr.observe({"type": "event", "event": "success", "job_id": "J1", "row_idx": 0,
                "iso_date": "d", "platform": "P", "payload": {}})
    pr.clear_acked([["J1", 0, "P"]])  # lists, as JSON decode produces
    assert pr.snapshot() == []


def test_snapshot_empty_initially():
    pr = PendingResults()
    assert pr.snapshot() == []


def test_multiple_distinct_keys_all_recorded():
    pr = PendingResults()
    pr.observe({"type": "event", "event": "success", "job_id": "J1", "row_idx": 0,
                "iso_date": "2026-05-22", "platform": "YouTube Video", "payload": {}})
    pr.observe({"type": "event", "event": "success", "job_id": "J1", "row_idx": 0,
                "iso_date": "2026-05-22", "platform": "Rock", "payload": {}})
    pr.observe({"type": "event", "event": "success", "job_id": "J1", "row_idx": 1,
                "iso_date": "2026-05-23", "platform": "YouTube Video", "payload": {}})
    assert len(pr.snapshot()) == 3
