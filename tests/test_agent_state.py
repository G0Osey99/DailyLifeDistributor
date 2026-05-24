"""Unit tests for agent.state.AgentState — the GUI/network bridge."""
from __future__ import annotations

import threading

import pytest

from agent import state as _st


def test_set_connection_and_snapshot_reflects_it():
    s = _st.AgentState()
    s.set_connection(_st.CONN_ONLINE, message="all good")
    snap = s.snapshot()
    assert snap["connection"] == _st.CONN_ONLINE
    assert snap["last_message"] == "all good"


def test_set_activity():
    s = _st.AgentState()
    s.set_activity(_st.ACT_UPLOADING, detail="YouTube · row 3/12")
    snap = s.snapshot()
    assert snap["activity"] == _st.ACT_UPLOADING
    assert snap["activity_detail"] == "YouTube · row 3/12"


def test_log_lines_capped_at_200():
    s = _st.AgentState()
    for i in range(250):
        s.append_log(f"line {i}")
    snap = s.snapshot()
    # deque(maxlen=200) drops the oldest.
    assert len(snap["log_lines"]) == 200
    assert snap["log_lines"][0] == "line 50"
    assert snap["log_lines"][-1] == "line 249"


def test_pairing_code_handshake_request_blocks_until_provide():
    s = _st.AgentState()
    received = {}

    def network_side():
        try:
            received["code"] = s.request_pairing_code()
        except RuntimeError as e:
            received["error"] = str(e)

    t = threading.Thread(target=network_side)
    t.start()
    # Give the thread a moment to enter the wait. Without a small sleep
    # we might race past needs_pairing_code being set.
    for _ in range(50):
        if s.snapshot()["needs_pairing_code"]:
            break
        import time as _t
        _t.sleep(0.01)
    assert s.snapshot()["needs_pairing_code"] is True

    s.provide_pairing_code("abc123")
    t.join(timeout=1.0)
    assert received == {"code": "abc123"}
    # Flag should have been cleared after the wait returned.
    assert s.snapshot()["needs_pairing_code"] is False


def test_pairing_code_cancel_raises_runtime_error():
    s = _st.AgentState()
    err = {}

    def network_side():
        try:
            s.request_pairing_code()
        except RuntimeError as e:
            err["msg"] = str(e)

    t = threading.Thread(target=network_side)
    t.start()
    for _ in range(50):
        if s.snapshot()["needs_pairing_code"]:
            break
        import time as _t
        _t.sleep(0.01)
    s.provide_pairing_code(None)  # cancel
    t.join(timeout=1.0)
    assert "msg" in err


def test_snapshot_is_independent_copy():
    """Mutating the snapshot's log_lines must not affect future snapshots."""
    s = _st.AgentState()
    s.append_log("one")
    snap1 = s.snapshot()
    snap1["log_lines"].append("forged")
    s.append_log("two")
    snap2 = s.snapshot()
    assert snap2["log_lines"] == ["one", "two"]
