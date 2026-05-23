# tests/test_phase3_relay_ingest.py
"""Task A6: relay-frame ingest — event frames routed to SSE queue."""
import queue
from core import agent_dispatch


def test_event_frame_routed_to_job_queue():
    q = queue.Queue()
    agent_dispatch.register_job(job_id="J1", sse_queue=q, session_id=None)
    agent_dispatch.on_frame({"v": 1, "type": "event", "job_id": "J1",
                             "row_idx": 0, "event": "upload_progress",
                             "platform": "YouTube Video", "percent": 42})
    msg = q.get_nowait()
    assert msg["event"] == "upload_progress"
    assert msg["row_idx"] == 0
    assert msg["percent"] == 42


def test_event_for_unknown_job_is_dropped_without_error():
    agent_dispatch.on_frame({"v": 1, "type": "event", "job_id": "missing",
                             "row_idx": 0, "event": "start"})
    # No exception raised — test passes if we reach here.


def test_event_frame_strips_envelope_fields():
    """v, type, job_id must NOT appear in the queued message."""
    q = queue.Queue()
    agent_dispatch.register_job(job_id="J2", sse_queue=q, session_id=None)
    agent_dispatch.on_frame({"v": 1, "type": "event", "job_id": "J2",
                             "event": "done", "row_idx": 1})
    msg = q.get_nowait()
    assert "v" not in msg
    assert "type" not in msg
    assert "job_id" not in msg
    assert msg["event"] == "done"
    assert msg["row_idx"] == 1


def test_drop_job_removes_from_registry():
    q = queue.Queue()
    agent_dispatch.register_job(job_id="J3", sse_queue=q)
    agent_dispatch.drop_job("J3")
    # After drop, event should be silently discarded (not raise).
    agent_dispatch.on_frame({"v": 1, "type": "event", "job_id": "J3",
                             "event": "start", "row_idx": 0})
    assert q.empty()


def test_unhandled_frame_type_is_no_op():
    """on_frame must not raise on unknown frame types (future A7/A8/A9)."""
    agent_dispatch.on_frame({"v": 1, "type": "credentials_updated",
                             "job_id": "J99"})
    agent_dispatch.on_frame({"v": 1, "type": "image_used", "job_id": "J99"})
    agent_dispatch.on_frame({"v": 1, "type": "pending_results_chunk",
                             "job_id": "J99"})


# ---------------------------------------------------------------------------
# Task A7: success events write to upload_history
# ---------------------------------------------------------------------------
def test_success_event_records_upload_history(temp_db):
    import queue
    from core import agent_dispatch, db as _db
    q = queue.Queue()
    agent_dispatch.register_job(job_id="J2", sse_queue=q, session_id="S1")
    agent_dispatch.on_frame({
        "v": 1, "type": "event", "job_id": "J2", "row_idx": 0,
        "event": "success", "platform": "YouTube Video",
        "iso_date": "2026-05-22", "payload": {"watch_url": "https://yt/x"},
    })
    assert _db.has_successful_upload("S1", "2026-05-22", "YouTube Video") is True


# ---------------------------------------------------------------------------
# Extra scope: send_to_device raises clearly when no default relay is set
# ---------------------------------------------------------------------------
def test_send_to_device_raises_when_no_default_relay(monkeypatch):
    """Misconfiguration (set_default_relay never called) must surface loudly."""
    from core import relay
    monkeypatch.setattr(relay, "_default_relay", None)
    import pytest
    with pytest.raises(RuntimeError, match="no default relay set"):
        relay.send_to_device("some-device", {"v": 1, "type": "ping"})


def test_send_to_device_works_when_default_relay_is_set(monkeypatch):
    """Smoke: send_to_device routes to the correct agent sink."""
    from core import relay
    sent = []

    # Build a minimal relay with one agent registered.
    r = relay.Relay()
    r.register_agent("default", "dev-abc", lambda msg: sent.append(msg))
    relay.set_default_relay(r, account="default")

    relay.send_to_device("dev-abc", {"v": 1, "type": "ping"})
    assert len(sent) == 1
    import json
    payload = json.loads(sent[0])
    assert payload["type"] == "ping"

    # Cleanup — restore None so other tests are not affected.
    monkeypatch.setattr(relay, "_default_relay", None)
