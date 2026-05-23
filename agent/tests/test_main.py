# agent/tests/test_main.py
"""Minimal tests for agent/main.py _on_message dispatch — B9."""
import pytest
from unittest.mock import MagicMock


def test_on_message_ping_sends_pong():
    from agent.main import _on_message
    conn = MagicMock()
    _on_message(conn, {"type": "ping", "payload": {"ts": 1}})
    conn.send.assert_called_once_with(
        {"v": 1, "type": "pong", "payload": {"ts": 1}}
    )


def test_on_message_job_plan_routes_to_dispatch_handle_job_plan(monkeypatch):
    """job_plan messages must be routed to dispatch.handle_job_plan."""
    from agent import dispatch, main

    called = {}

    def _fake_handle(*, plan, transport):
        called["plan"] = plan
        called["transport"] = transport

    monkeypatch.setattr(dispatch, "handle_job_plan", _fake_handle)

    conn = MagicMock()
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

    assert called["plan"] is plan, "handle_job_plan did not receive the plan"
    # Transport wraps conn.send — verify it delegates correctly.
    called["transport"].send({"type": "event", "event": "done"})
    conn.send.assert_called_once_with({"type": "event", "event": "done"})


def test_on_message_job_plan_catches_exception_without_crashing(monkeypatch):
    """A crash in handle_job_plan must be caught; _on_message must not raise."""
    from agent import dispatch, main

    def _bad_handle(*, plan, transport):
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(dispatch, "handle_job_plan", _bad_handle)

    conn = MagicMock()
    # Must not raise.
    main._on_message(conn, {"type": "job_plan", "job_id": "J0",
                            "rows": [], "credentials": {}, "config": {}})
