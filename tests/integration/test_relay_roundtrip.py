"""Browser <-> VPS relay <-> agent control round-trip over real WebSockets."""
import json
import threading
import time

import pytest

simple_websocket = pytest.importorskip("simple_websocket")


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    # Multi-tenant phase α: legacy shared-password form (opt-in).
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
    import importlib
    import core.db as db
    import core.devices as devices
    importlib.reload(db); importlib.reload(devices); db.init_db()
    from core import auth
    auth.reset_lockouts(); auth.set_password("pw")
    import app as flask_app_module
    importlib.reload(flask_app_module)
    app = flask_app_module.app
    app.config["TESTING"] = True

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    # ThreadingMixIn.daemon_threads defaults to False, so any websocket
    # connection still open at teardown keeps Python from exiting and
    # hangs the whole CI suite. Make request-handler threads daemons.
    srv.daemon_threads = True
    port = srv.socket.getsockname()[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    yield {"app": app, "port": port, "devices": devices}
    srv.shutdown()


def test_ping_pong_through_relay(live_server):
    port = live_server["port"]
    devices = live_server["devices"]

    code = devices.create_pairing_code()
    device_id, token = devices.redeem_pairing_code(code, "Mac")

    import requests
    s = requests.Session()
    s.post(f"http://127.0.0.1:{port}/login", data={"password": "pw"})
    cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())

    # Connect agent first, then poll the relay until register_agent has
    # actually run on the server thread (replacing the fragile 0.3s sleep
    # that races on slow CI runners — that race produced the None →
    # json.loads(None) TypeErrors in run 26363413161).
    agent = simple_websocket.Client(
        f"ws://127.0.0.1:{port}/agent/socket?token={token}")
    from core import relay as _relay
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if _relay.online_agent_count() >= 1:
            break
        time.sleep(0.05)
    else:
        agent.close()
        pytest.fail("agent never registered with relay within 10s")

    browser = simple_websocket.Client(
        f"ws://127.0.0.1:{port}/agent/ws",
        headers={"Cookie": cookie})
    try:
        # Receive messages until we see online=True (handles the rare case
        # where the browser got a online=False presence before the agent
        # registered, then a second online=True broadcast after). Guard
        # against receive() returning None on timeout.
        for _ in range(3):
            raw = browser.receive(timeout=5)
            if raw is None:
                pytest.fail("relay sent no presence frame within 5s")
            presence = json.loads(raw)
            assert presence["type"] == "presence"
            if presence["payload"]["online"] is True:
                break
        else:
            pytest.fail(f"Never received online=True presence; last: {presence}")

        browser.send(json.dumps({"v": 1, "type": "ping", "payload": {"x": 1}}))
        raw = agent.receive(timeout=5)
        assert raw is not None, "agent never received the ping within 5s"
        got = json.loads(raw)
        assert got["type"] == "ping" and got["payload"]["x"] == 1
        agent.send(json.dumps({"v": 1, "type": "pong", "payload": {"x": 1}}))
        raw = browser.receive(timeout=5)
        assert raw is not None, "browser never received the pong within 5s"
        pong = json.loads(raw)
        assert pong["type"] == "pong" and pong["payload"]["x"] == 1
    finally:
        agent.close(); browser.close()
