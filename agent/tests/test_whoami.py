"""Tests for the agent's whoami_ping / whoami_pong handler."""
from __future__ import annotations

import pytest

from agent import config, main as agent_main


class _FakeConn:
    """Stand-in for AgentConnection: captures every .send() payload."""
    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)


def test_whoami_ping_emits_pong_with_required_fields(monkeypatch):
    monkeypatch.setattr(config, "get_device_id", lambda: "dev-xyz")
    monkeypatch.setattr(agent_main._hwid_mod, "compute_hwid_hash", lambda: "a" * 64)
    monkeypatch.setattr(agent_main._hostname_mod, "get_friendly_hostname",
                        lambda: "Studio")

    conn = _FakeConn()
    agent_main._on_message(conn, {
        "v": 1, "type": "whoami_ping", "ping_id": "ping-1",
    })
    assert len(conn.sent) == 1
    pong = conn.sent[0]
    assert pong["type"] == "whoami_pong"
    assert pong["ping_id"] == "ping-1"
    assert pong["device_id"] == "dev-xyz"
    assert pong["hwid_hash"] == "a" * 64
    assert pong["hostname"] == "Studio"
    assert pong["v"] == 1
    assert "protocol_version" in pong


def test_whoami_pong_echoes_ping_id(monkeypatch):
    """Each pong must echo the originating ping_id verbatim so the browser
    can correlate it with its own outstanding ping."""
    monkeypatch.setattr(config, "get_device_id", lambda: "dev1")
    monkeypatch.setattr(agent_main._hwid_mod, "compute_hwid_hash", lambda: "a" * 64)
    monkeypatch.setattr(agent_main._hostname_mod, "get_friendly_hostname",
                        lambda: "host")

    conn = _FakeConn()
    agent_main._on_message(conn, {
        "v": 1, "type": "whoami_ping", "ping_id": "uniq-abc-123",
    })
    assert conn.sent[0]["ping_id"] == "uniq-abc-123"


def test_whoami_pong_handles_missing_ping_id(monkeypatch):
    """If a ping arrives without ping_id (older client), pong includes an
    empty string rather than KeyErroring."""
    monkeypatch.setattr(config, "get_device_id", lambda: "dev1")
    monkeypatch.setattr(agent_main._hwid_mod, "compute_hwid_hash", lambda: "a" * 64)
    monkeypatch.setattr(agent_main._hostname_mod, "get_friendly_hostname",
                        lambda: "host")

    conn = _FakeConn()
    agent_main._on_message(conn, {"v": 1, "type": "whoami_ping"})
    assert conn.sent[0]["ping_id"] == ""


def test_whoami_pong_when_device_id_unknown(monkeypatch):
    """If the agent never stored a device_id (older pair flow), pong returns
    empty-string for device_id rather than crashing."""
    monkeypatch.setattr(config, "get_device_id", lambda: None)
    monkeypatch.setattr(agent_main._hwid_mod, "compute_hwid_hash", lambda: "a" * 64)
    monkeypatch.setattr(agent_main._hostname_mod, "get_friendly_hostname",
                        lambda: "host")

    conn = _FakeConn()
    agent_main._on_message(conn, {
        "v": 1, "type": "whoami_ping", "ping_id": "p1",
    })
    assert conn.sent[0]["device_id"] == ""


def test_whoami_pong_resilient_to_hwid_failure(monkeypatch):
    """If compute_hwid_hash() somehow raises, the pong still gets emitted
    with hwid_hash='' so the browser's ping isn't silently dropped."""
    monkeypatch.setattr(config, "get_device_id", lambda: "dev1")

    def _boom():
        raise RuntimeError("hwid failure")

    monkeypatch.setattr(agent_main._hwid_mod, "compute_hwid_hash", _boom)
    monkeypatch.setattr(agent_main._hostname_mod, "get_friendly_hostname",
                        lambda: "host")

    conn = _FakeConn()
    agent_main._on_message(conn, {
        "v": 1, "type": "whoami_ping", "ping_id": "p1",
    })
    assert len(conn.sent) == 1
    assert conn.sent[0]["hwid_hash"] == ""
    assert conn.sent[0]["hostname"] == "host"


def test_whoami_pong_resilient_to_hostname_failure(monkeypatch):
    monkeypatch.setattr(config, "get_device_id", lambda: "dev1")
    monkeypatch.setattr(agent_main._hwid_mod, "compute_hwid_hash", lambda: "a" * 64)

    def _boom():
        raise RuntimeError("hostname failure")

    monkeypatch.setattr(agent_main._hostname_mod, "get_friendly_hostname", _boom)

    conn = _FakeConn()
    agent_main._on_message(conn, {
        "v": 1, "type": "whoami_ping", "ping_id": "p1",
    })
    assert len(conn.sent) == 1
    assert conn.sent[0]["hostname"] == ""
    assert conn.sent[0]["hwid_hash"] == "a" * 64
