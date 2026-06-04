"""Keepalive must send a PING control frame on a fixed cadence even while a
job is streaming data frames.

Regression: the ping was gated on time-since-ANY-send, so an active upload
(constant progress data frames) kept that timer fresh and NO ping ever fired
— and Cloudflare Tunnel (which only honors control frames) closed the socket
~24s in, producing a reconnect storm during every job. The ping must be gated
on time-since-last-PING instead.
"""
from __future__ import annotations

import time

from agent.transport import AgentConnection, _PING_INTERVAL_S


class _FakeInner:
    """Stands in for simple_websocket.Client. receive() returns None (poll
    timeout) and ends the loop; send() records that a frame went out."""

    def __init__(self, conn):
        self._conn = conn
        self.sends = 0

    # _send_ping does: self.ws.ws.send(Ping()) then self.ws.sock.send(out)
    @property
    def ws(self):
        return self

    @property
    def sock(self):
        return self

    def receive(self, timeout=None):
        # Let exactly one inner iteration run, then exit the loop.
        self._conn._shutdown.set()
        return None

    def send(self, data=None):
        self.sends += 1
        return b"x"


def _on_message(_msg):  # pragma: no cover - never called (receive returns None)
    raise AssertionError("on_message should not be called on a poll timeout")


def test_ping_fires_when_data_recent_but_ping_overdue():
    conn = AgentConnection("https://x", "tok")
    conn.ws = _FakeInner(conn)
    now = time.monotonic()
    # Simulate mid-job: a data frame went out moments ago, but the last PING
    # was a full interval+ ago.
    conn._last_send_at = now
    conn._last_ping_at = now - (_PING_INTERVAL_S + 5.0)

    conn.run_once(_on_message)

    # The keepalive must have sent a ping and advanced the ping clock.
    assert conn.ws.sends >= 1
    assert conn._last_ping_at > now - 1.0


def test_ping_not_resent_when_recently_pinged():
    conn = AgentConnection("https://x", "tok")
    conn.ws = _FakeInner(conn)
    now = time.monotonic()
    conn._last_send_at = now - 100.0   # data idle a long time
    conn._last_ping_at = now           # but we just pinged

    conn.run_once(_on_message)

    # No new ping — the cadence is governed by _last_ping_at, which is fresh.
    assert conn.ws.sends == 0
