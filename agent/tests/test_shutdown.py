"""Tests for graceful shutdown behaviour — signal handler + transport loop exit.

We avoid calling signal.raise_signal (unreliable in pytest on Windows).
Instead we test the actual behaviour we care about:
  1. The signal handler sets _shutdown_event when invoked directly.
  2. AgentConnection.run_once() returns False when shutdown_event is set,
     even if no message arrives (i.e. the loop exits without blocking).
  3. run() exits its loop when shutdown_event is set before connecting,
     so a test can drive the full run() path without a real server.
"""
from __future__ import annotations

import signal
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Signal handler sets _shutdown_event
# ---------------------------------------------------------------------------

def test_install_signal_handlers_sets_shutdown_event():
    """Calling the installed SIGINT handler must set _shutdown_event."""
    import agent.main as main_mod

    # Reset the module-level event so prior tests don't interfere.
    main_mod._shutdown_event.clear()

    main_mod._install_signal_handlers()

    # Retrieve the installed handler and call it directly (no actual signal).
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler), "SIGINT handler should be callable"

    handler(signal.SIGINT, None)

    assert main_mod._shutdown_event.is_set(), (
        "_shutdown_event must be set after the SIGINT handler fires"
    )

    # Clean up: restore default SIGINT and clear the event for other tests.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    main_mod._shutdown_event.clear()


# ---------------------------------------------------------------------------
# 2. AgentConnection.run_once exits when shutdown_event is set
# ---------------------------------------------------------------------------

def test_run_once_returns_false_when_shutdown_set():
    """run_once must return False immediately when shutdown_event is set,
    without waiting for a message from the server."""
    from agent.transport import AgentConnection

    shutdown = threading.Event()
    shutdown.set()  # already shut down

    conn = AgentConnection("https://example.com", "tok", shutdown_event=shutdown)

    # Attach a fake ws that would block forever if actually called.
    fake_ws = MagicMock()
    # receive() should never be called because shutdown is pre-set.
    conn.ws = fake_ws

    on_message = MagicMock()
    result = conn.run_once(on_message)

    assert result is False, "run_once must return False when shutdown_event is set"
    on_message.assert_not_called()
    fake_ws.receive.assert_not_called()


def test_run_once_returns_false_on_timeout_then_shutdown():
    """run_once must keep looping on timeout=None returning None (idle server),
    then exit when shutdown_event is set between polls."""
    from agent.transport import AgentConnection

    shutdown = threading.Event()
    conn = AgentConnection("https://example.com", "tok", shutdown_event=shutdown)

    call_count = 0

    def _fake_receive(timeout):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            # On the second poll, signal shutdown.
            shutdown.set()
        return None  # simulate timeout (no message)

    fake_ws = MagicMock()
    fake_ws.receive.side_effect = _fake_receive
    conn.ws = fake_ws

    on_message = MagicMock()
    result = conn.run_once(on_message)

    assert result is False
    on_message.assert_not_called()
    assert call_count >= 1


def test_run_once_processes_message_then_returns_true():
    """run_once must dispatch a message and return True in the normal case."""
    from agent.transport import AgentConnection

    shutdown = threading.Event()
    conn = AgentConnection("https://example.com", "tok", shutdown_event=shutdown)

    fake_ws = MagicMock()
    fake_ws.receive.return_value = '{"type": "ping", "payload": {}}'
    conn.ws = fake_ws

    received = []
    result = conn.run_once(received.append)

    assert result is True
    assert len(received) == 1
    assert received[0] == {"type": "ping", "payload": {}}


# ---------------------------------------------------------------------------
# 3. run() exits loop when shutdown_event pre-set (no real server needed)
# ---------------------------------------------------------------------------

def test_run_exits_when_shutdown_event_pre_set(monkeypatch):
    """run() must exit its while-loop immediately when the shutdown_event is
    already set, without trying to connect."""
    import agent.main as main_mod

    shutdown = threading.Event()
    shutdown.set()

    # Stub out pairing — pretend we already have a token.
    monkeypatch.setattr(main_mod.config, "get_token", lambda: "fake-token")
    # Stub out updater so it's a no-op.
    monkeypatch.setattr(main_mod.updater, "check_and_apply", lambda url: None)

    connect_called = []

    class _FakeConn:
        def __init__(self, *a, **kw):
            pass

        def connect(self, *a, **kw):
            connect_called.append(True)

        def run_once(self, *a, **kw):
            return False

        def close(self):
            pass

    monkeypatch.setattr(main_mod, "AgentConnection", _FakeConn)

    # Should return quickly (not block).
    main_mod.run("https://example.com", shutdown_event=shutdown)

    # The loop condition is checked before connect(), so connect should not
    # have been called.
    assert not connect_called, "connect() must not be called when shutdown is pre-set"
