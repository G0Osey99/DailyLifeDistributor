# agent/tests/test_main.py
"""Minimal tests for agent/main.py _on_message dispatch — B9 + Phase 3."""
from unittest.mock import MagicMock


def test_on_message_ping_sends_pong():
    from agent.main import _on_message
    conn = MagicMock()
    _on_message(conn, {"type": "ping", "payload": {"ts": 1}})
    conn.send.assert_called_once_with(
        {"v": 1, "type": "pong", "payload": {"ts": 1}}
    )


def test_on_message_job_plan_routes_to_dispatch_handle_job_plan(monkeypatch):
    """job_plan messages must be routed to dispatch.handle_job_plan on a
    background thread (so the receive loop stays responsive while the
    upload runs)."""
    import threading
    from agent import dispatch, main

    # Ensure the busy slot is clear before we dispatch.
    with main._active_job_lock:
        main._active_job_id = None

    called = {}
    done = threading.Event()

    def _fake_handle(*, plan, transport):
        called["plan"] = plan
        called["transport"] = transport
        done.set()

    monkeypatch.setattr(dispatch, "handle_job_plan", _fake_handle)

    conn = MagicMock()
    # In production the run() loop sets _current_conn to the live connection
    # after connect; the job transport emits through it (so events survive a
    # mid-job reconnect). Mirror that here so the wrapper has a live target.
    monkeypatch.setattr(main, "_current_conn", conn)
    plan = {
        "v": 1,
        "type": "job_plan",
        "job_id": "J99",
        "protocol_version": 1,
        "config": {"max_workers": 2},
        "rows": [],
        "credentials": {},
    }
    main._on_message(conn, plan)

    # The dispatch happens on a daemon thread; wait briefly for it.
    assert done.wait(2.0), "handle_job_plan was not invoked on a background thread"
    assert called["plan"] is plan, "handle_job_plan did not receive the plan"
    # Transport wraps conn.send — verify it delegates correctly.
    called["transport"].send({"type": "event", "event": "done"})
    # Allow the worker finally-block to clear _active_job_id.
    for _ in range(20):
        with main._active_job_lock:
            if main._active_job_id is None:
                break
        threading.Event().wait(0.05)


def test_job_transport_follows_current_conn_across_reconnect(monkeypatch):
    """A job's transport must emit through the CURRENT connection, so events
    survive a mid-job WebSocket reconnect (the old conn is replaced, not
    repopulated). Regression for the 'NoneType has no attribute send' flood."""
    import threading
    from agent import dispatch, main

    with main._active_job_lock:
        main._active_job_id = None

    captured = {}
    done = threading.Event()

    def _capture_handle(*, plan, transport):
        captured["transport"] = transport
        done.set()

    monkeypatch.setattr(dispatch, "handle_job_plan", _capture_handle)

    conn1 = MagicMock(name="conn1")
    conn2 = MagicMock(name="conn2")
    monkeypatch.setattr(main, "_current_conn", conn1)

    main._on_message(conn1, {"type": "job_plan", "job_id": "JR",
                             "rows": [], "credentials": {}, "config": {}})
    assert done.wait(2.0)
    transport = captured["transport"]

    # First emit goes to the live conn1.
    transport.send({"type": "event", "event": "start"})
    assert conn1.send.call_count == 1

    # Simulate a reconnect: run() swaps _current_conn to a brand-new conn2.
    monkeypatch.setattr(main, "_current_conn", conn2)
    transport.send({"type": "event", "event": "done"})
    # The post-reconnect frame must land on conn2, NOT the dead conn1.
    assert conn2.send.call_count == 1
    assert conn1.send.call_count == 1

    with main._active_job_lock:
        main._active_job_id = None


def test_on_message_job_plan_catches_exception_without_crashing(monkeypatch):
    """A crash in handle_job_plan must be caught on the worker thread;
    _on_message must not raise (the thread itself logs the exception)."""
    import threading
    from agent import dispatch, main

    with main._active_job_lock:
        main._active_job_id = None

    crashed = threading.Event()

    def _bad_handle(*, plan, transport):
        try:
            raise RuntimeError("dispatch exploded")
        finally:
            crashed.set()

    monkeypatch.setattr(dispatch, "handle_job_plan", _bad_handle)

    conn = MagicMock()
    # Must not raise.
    main._on_message(conn, {"type": "job_plan", "job_id": "J0",
                            "rows": [], "credentials": {}, "config": {}})
    assert crashed.wait(2.0)
    # Slot must be released even after a crash.
    for _ in range(20):
        with main._active_job_lock:
            if main._active_job_id is None:
                break
        threading.Event().wait(0.05)
    with main._active_job_lock:
        assert main._active_job_id is None


def test_on_message_job_plan_busy_rejects_second_job(monkeypatch):
    """A second job_plan arriving while another is running is rejected
    with an error event and does NOT spawn a second dispatch."""
    import threading
    from agent import dispatch, main

    with main._active_job_lock:
        main._active_job_id = None

    started = threading.Event()
    release = threading.Event()
    calls = []

    def _slow_handle(*, plan, transport):
        calls.append(plan["job_id"])
        started.set()
        release.wait(2.0)

    monkeypatch.setattr(dispatch, "handle_job_plan", _slow_handle)

    conn = MagicMock()
    plan_a = {"type": "job_plan", "job_id": "JA",
              "rows": [], "credentials": {}, "config": {}}
    plan_b = {"type": "job_plan", "job_id": "JB",
              "rows": [], "credentials": {}, "config": {}}

    main._on_message(conn, plan_a)
    assert started.wait(2.0), "first job didn't start"

    # Second job arrives while first is still running.
    main._on_message(conn, plan_b)
    # The busy-rejection should have sent an error frame with JB's id.
    error_sends = [c for c in conn.send.call_args_list
                   if c.args and c.args[0].get("type") == "event"
                   and c.args[0].get("event") == "error"
                   and c.args[0].get("job_id") == "JB"]
    assert error_sends, f"no busy-rejection frame for JB sent: {conn.send.call_args_list}"

    # Let JA finish and confirm only one dispatch happened.
    release.set()
    for _ in range(40):
        with main._active_job_lock:
            if main._active_job_id is None:
                break
        threading.Event().wait(0.05)
    assert calls == ["JA"], f"expected only JA to dispatch, got {calls}"
