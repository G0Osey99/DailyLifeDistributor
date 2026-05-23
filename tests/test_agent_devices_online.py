"""Tests for GET /agent/devices/online — Phase 3.5 device picker endpoint."""
from __future__ import annotations

import importlib
import pytest

from core import auth


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


def _login(c):
    c.post("/login", data={"password": "correct-horse"})


def _pair_and_register_agent(client, *, device_name="Mac",
                              hwid_hash=None, hostname=None,
                              connect_ip="1.2.3.4"):
    """Helper: create a device row + register it on the relay so it shows
    up as 'online' to the list endpoint."""
    import core.devices as devices
    code = devices.create_pairing_code()
    device_id, _ = devices.redeem_pairing_code(
        code, device_name, hwid_hash=hwid_hash, hostname=hostname)
    # Manually register on the in-process relay (we don't open a real ws).
    from blueprints.agent import RELAY, _ACCOUNT
    RELAY.register_agent(_ACCOUNT, device_id, lambda _m: None,
                         device_name=device_name, connect_ip=connect_ip)
    return device_id


def test_online_endpoint_returns_empty_when_no_agent(client):
    _login(client)
    resp = client.get("/agent/devices/online")
    assert resp.status_code == 200
    assert resp.get_json() == {"devices": []}


def test_online_endpoint_lists_connected_agent(client):
    _login(client)
    device_id = _pair_and_register_agent(
        client, device_name="Mac", hwid_hash="a" * 64, hostname="Studio")
    try:
        resp = client.get("/agent/devices/online")
        assert resp.status_code == 200
        devs = resp.get_json()["devices"]
        assert len(devs) == 1
        d = devs[0]
        assert d["id"] == device_id
        assert d["name"] == "Mac"
        assert d["hostname"] == "Studio"
        assert d["hwid_hash_short"] == "a" * 8
        assert "last_seen_at" in d
        assert "same_network" in d
    finally:
        from blueprints.agent import RELAY, _ACCOUNT
        RELAY.unregister_agent(_ACCOUNT, device_id)


def test_online_endpoint_same_network_true_when_ips_match(client, monkeypatch):
    _login(client)
    # Register the agent with a known IP, then arrange for _client_ip()
    # (during the GET) to return the same value via CF-Connecting-IP.
    device_id = _pair_and_register_agent(
        client, hwid_hash="b" * 64, connect_ip="10.0.0.5")
    try:
        resp = client.get("/agent/devices/online", headers={
            "CF-Connecting-IP": "10.0.0.5",
        })
        assert resp.status_code == 200
        devs = resp.get_json()["devices"]
        assert devs[0]["same_network"] is True
    finally:
        from blueprints.agent import RELAY, _ACCOUNT
        RELAY.unregister_agent(_ACCOUNT, device_id)


def test_online_endpoint_same_network_false_when_ips_differ(client):
    _login(client)
    device_id = _pair_and_register_agent(
        client, hwid_hash="c" * 64, connect_ip="10.0.0.5")
    try:
        resp = client.get("/agent/devices/online", headers={
            "CF-Connecting-IP": "8.8.8.8",
        })
        assert resp.status_code == 200
        devs = resp.get_json()["devices"]
        assert devs[0]["same_network"] is False
    finally:
        from blueprints.agent import RELAY, _ACCOUNT
        RELAY.unregister_agent(_ACCOUNT, device_id)


def test_online_endpoint_same_network_false_when_browser_ip_unknown(client):
    """If the browser IP can't be determined, same_network must be False
    (never default-True on a missing signal)."""
    _login(client)
    # Agent connect_ip='unknown' or matching 'unknown' must not collapse to
    # same_network=True. Use the unknown branch: empty headers + blank REMOTE.
    device_id = _pair_and_register_agent(
        client, hwid_hash="d" * 64, connect_ip="unknown")
    try:
        # No CF, no XFF, REMOTE_ADDR populated by test_client (127.0.0.1).
        resp = client.get("/agent/devices/online")
        assert resp.status_code == 200
        devs = resp.get_json()["devices"]
        assert devs[0]["same_network"] is False
    finally:
        from blueprints.agent import RELAY, _ACCOUNT
        RELAY.unregister_agent(_ACCOUNT, device_id)


def test_online_endpoint_lists_multiple_agents(client):
    _login(client)
    a = _pair_and_register_agent(client, device_name="Mac",
                                  hwid_hash="a" * 64, connect_ip="1.1.1.1")
    b = _pair_and_register_agent(client, device_name="Studio",
                                  hwid_hash="b" * 64, connect_ip="2.2.2.2")
    try:
        resp = client.get("/agent/devices/online")
        assert resp.status_code == 200
        devs = resp.get_json()["devices"]
        ids = {d["id"] for d in devs}
        assert a in ids
        assert b in ids
        assert len(devs) == 2
    finally:
        from blueprints.agent import RELAY, _ACCOUNT
        RELAY.unregister_agent(_ACCOUNT, a)
        RELAY.unregister_agent(_ACCOUNT, b)


def test_online_endpoint_requires_auth(client):
    """Without a login session, the endpoint redirects to /login (HTML) or
    401s (JSON-accepted)."""
    resp = client.get("/agent/devices/online")
    # No login → either redirect or 401 depending on the Accept header
    assert resp.status_code in (302, 401)


def test_online_endpoint_returns_401_when_json_requested_and_unauth(client):
    """JSON-accepted unauthenticated request → 401 (not redirect)."""
    resp = client.get("/agent/devices/online",
                       headers={"Accept": "application/json"})
    assert resp.status_code == 401


def test_online_endpoint_returns_short_hwid_8_chars(client):
    """hwid_hash_short must be exactly 8 chars (or None)."""
    _login(client)
    device_id = _pair_and_register_agent(
        client, hwid_hash="abcdef0123456789" + "0" * 48)
    try:
        resp = client.get("/agent/devices/online")
        devs = resp.get_json()["devices"]
        assert devs[0]["hwid_hash_short"] == "abcdef01"
    finally:
        from blueprints.agent import RELAY, _ACCOUNT
        RELAY.unregister_agent(_ACCOUNT, device_id)


def test_online_endpoint_hwid_short_is_none_when_missing(client):
    """Agent without recorded hwid_hash (back-compat) → hwid_hash_short None."""
    _login(client)
    device_id = _pair_and_register_agent(client)  # no hwid_hash
    try:
        resp = client.get("/agent/devices/online")
        devs = resp.get_json()["devices"]
        assert devs[0]["hwid_hash_short"] is None
    finally:
        from blueprints.agent import RELAY, _ACCOUNT
        RELAY.unregister_agent(_ACCOUNT, device_id)
