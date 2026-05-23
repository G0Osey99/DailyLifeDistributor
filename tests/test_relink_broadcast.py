"""POST /agent/pair/redeem broadcasts a `relinked` event when the agent
re-pairs onto an existing HWID.

The browser dashboard subscribes to /agent/ws and surfaces a toast on the
``relinked`` frame so the user sees "Re-linked agent <name> (previously
<old-name>)" without having to refresh the page.
"""
from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    import core.db as db
    import core.devices as devices
    importlib.reload(db)
    importlib.reload(devices)
    db.init_db()
    from core import auth
    auth.reset_lockouts()
    auth.set_password("correct-horse")
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def _login(c):
    c.post("/login", data={"password": "correct-horse"})


def _attach_browser_sink(account: str = "default") -> list[str]:
    """Register a fake browser sink on the process-wide RELAY and return
    the list it appends to."""
    from blueprints import agent as agent_bp
    captured: list[str] = []
    agent_bp.RELAY.register_browser(account, "test-browser-1", captured.append)
    return captured


def test_pair_redeem_relink_broadcasts_event(client):
    """When pair_redeem detects an HWID match (relink path), every connected
    browser receives a ``{"v":1,"type":"relinked","payload":{...}}`` frame
    carrying the new device's name, previous name, and id."""
    _login(client)
    captured = _attach_browser_sink()
    # register_browser sends a presence frame immediately; ignore it.
    captured.clear()

    # First pairing — establishes the row with hwid + a friendly name.
    code1 = client.post("/agent/pair/new").get_json()["code"]
    client.application.test_client().post(
        "/agent/pair/redeem", json={
            "code": code1, "name": "First Studio",
            "hwid_hash": "a" * 64, "hostname": "studio.local",
        })
    devs = client.get("/agent/devices").get_json()["devices"]
    old_id = devs[0]["id"]
    # User renames the original device.
    client.post(f"/agent/devices/{old_id}/name", json={"name": "Studio Mac"})

    # Second pairing — same HWID. Triggers the relink path.
    code2 = client.post("/agent/pair/new").get_json()["code"]
    captured.clear()  # ignore any presence/whoami noise from intermediate calls
    resp = client.application.test_client().post(
        "/agent/pair/redeem", json={
            "code": code2, "name": "fresh-default",
            "hwid_hash": "a" * 64, "hostname": "studio.local",
        })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["relinked"] is True
    new_id = body["device_id"]

    # The relink broadcast must have landed on our sink.
    relinked_frames = []
    for raw in captured:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if msg.get("type") == "relinked":
            relinked_frames.append(msg)
    assert len(relinked_frames) == 1, (
        f"expected exactly one relinked frame, got {relinked_frames!r}")
    frame = relinked_frames[0]
    assert frame["v"] == 1
    payload = frame["payload"]
    assert payload["device_id"] == new_id
    assert payload["previous_name"] == "Studio Mac"
    # The server overrides the agent's "fresh-default" name with the
    # inherited friendly name, so new_name == previous_name on relink.
    assert payload["new_name"] == "Studio Mac"


def test_pair_redeem_non_relink_does_not_broadcast(client):
    """A first-time pair (no prior HWID match) emits no `relinked` event."""
    _login(client)
    captured = _attach_browser_sink()
    captured.clear()

    code = client.post("/agent/pair/new").get_json()["code"]
    resp = client.application.test_client().post(
        "/agent/pair/redeem", json={
            "code": code, "name": "Fresh", "hwid_hash": "b" * 64,
        })
    assert resp.status_code == 200
    body = resp.get_json()
    assert "relinked" not in body

    for raw in captured:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        assert msg.get("type") != "relinked", (
            f"unexpected relinked frame on a fresh pair: {msg!r}")


def test_relay_broadcast_to_browsers_sends_to_all(monkeypatch):
    """Unit test the new Relay.broadcast_to_browsers helper independently:
    multiple browser sinks all receive the same frame; a broken sink
    doesn't take the rest down."""
    from core import relay as relay_mod
    r = relay_mod.Relay()
    received_a: list[str] = []
    received_b: list[str] = []

    broken_state = {"raise": False}

    def _broken_sink(_msg):
        if broken_state["raise"]:
            raise RuntimeError("simulated send failure")
        # During register_browser the presence frame goes through this sink
        # first; let it succeed so registration completes.

    r.register_browser("default", "browser-a", received_a.append)
    r.register_browser("default", "browser-b", received_b.append)
    r.register_browser("default", "browser-broken", _broken_sink)
    # register_browser sends a presence frame on connect; clear them.
    received_a.clear()
    received_b.clear()
    # Now activate the failure mode for the broadcast under test.
    broken_state["raise"] = True

    r.broadcast_to_browsers("default", "relinked",
                            {"device_id": "X", "new_name": "n",
                             "previous_name": "p"})

    assert len(received_a) == 1
    assert len(received_b) == 1
    msg = json.loads(received_a[0])
    assert msg["type"] == "relinked"
    assert msg["payload"] == {"device_id": "X", "new_name": "n",
                              "previous_name": "p"}
    # Both healthy sinks see the same frame.
    assert received_a[0] == received_b[0]
