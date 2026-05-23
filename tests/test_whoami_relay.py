"""Tests for the server-side whoami_ping forwarding through the relay.

The relay doesn't parse whoami_ping/pong frames; they ride the existing
route_from_browser / route_from_agent paths. These tests confirm that
the forwarding works end-to-end and that the pair_redeem response now
carries device_id (the agent needs it to self-identify in pong frames).
"""
from __future__ import annotations

import importlib
import json
import pytest

from core import auth
from core.relay import Relay


class _Sink:
    def __init__(self):
        self.sent = []

    def __call__(self, text):
        self.sent.append(text)


def test_whoami_ping_routed_browser_to_agent():
    """A whoami_ping from the browser must be forwarded to the agent verbatim."""
    r = Relay()
    agent = _Sink()
    browser = _Sink()
    r.register_agent("acct", "dev1", agent, connect_ip="1.2.3.4")
    r.register_browser("acct", "sess1", browser)
    msg = json.dumps({"v": 1, "type": "whoami_ping", "ping_id": "p1"})
    r.route_from_browser("acct", msg)
    # The agent's sink received the ping. (browser.sent may contain the
    # presence broadcast from register_browser — we don't care about that.)
    assert msg in agent.sent


def test_whoami_pong_routed_agent_to_browser():
    """A whoami_pong from the agent must be forwarded to all browsers."""
    r = Relay()
    agent = _Sink()
    browser = _Sink()
    r.register_agent("acct", "dev1", agent)
    r.register_browser("acct", "sess1", browser)
    browser.sent.clear()
    pong = json.dumps({
        "v": 1, "type": "whoami_pong", "ping_id": "p1",
        "device_id": "dev1", "hwid_hash": "a" * 64, "hostname": "Studio",
        "protocol_version": "0.5.0",
    })
    r.route_from_agent("acct", pong)
    assert browser.sent == [pong]


def test_whoami_ping_broadcast_to_multiple_agents():
    """When multiple agents are connected, a ping reaches all of them
    (each pong is then routed back to the originating browser)."""
    r = Relay()
    a1 = _Sink(); a2 = _Sink()
    r.register_agent("acct", "dev1", a1, connect_ip="1.2.3.4")
    r.register_agent("acct", "dev2", a2, connect_ip="5.6.7.8")
    browser = _Sink()
    r.register_browser("acct", "sess1", browser)
    msg = json.dumps({"v": 1, "type": "whoami_ping", "ping_id": "p1"})
    r.route_from_browser("acct", msg)
    assert msg in a1.sent
    assert msg in a2.sent


# ---------------------------------------------------------------------------
# pair_redeem now returns device_id alongside token (the agent stores it).
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    import core.db as db
    import core.devices as devices
    importlib.reload(db)
    importlib.reload(devices)
    db.init_db()
    auth.reset_lockouts()
    auth.set_password("correct-horse")
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def test_pair_redeem_returns_device_id_for_agent_storage(client):
    """The redeem response must include device_id so the agent can
    persist it for whoami_pong frames."""
    client.post("/login", data={"password": "correct-horse"})
    code = client.post("/agent/pair/new").get_json()["code"]
    client2 = client.application.test_client()
    resp = client2.post("/agent/pair/redeem", json={
        "code": code, "name": "Mac",
        "hwid_hash": "a" * 64, "hostname": "Studio",
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert "device_id" in body
    assert body["device_id"]  # non-empty
    assert "token" in body
