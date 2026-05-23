"""End-to-end: real server + agent.main pong logic + browser ws client."""
import json
import threading
import time

import pytest

simple_websocket = pytest.importorskip("simple_websocket")


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    # Multi-tenant phase α: integration test logs in via the shared-password
    # form, which is now opt-in behind LEGACY_PASSWORD_ENABLED.
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
    import importlib
    import core.db as db, core.devices as devices
    importlib.reload(db); importlib.reload(devices); db.init_db()
    from core import auth
    auth.reset_lockouts(); auth.set_password("pw")
    import app as m; importlib.reload(m)
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, m.app, threaded=True)
    port = srv.socket.getsockname()[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield port, devices
    srv.shutdown()


def test_agent_main_pong(live, monkeypatch):
    port, devices = live
    server_url = f"http://127.0.0.1:{port}"

    code = devices.create_pairing_code()
    _, token = devices.redeem_pairing_code(code, "Mac")
    from agent import config
    monkeypatch.setattr(config, "get_token", lambda: token)
    monkeypatch.setattr(config, "get_server_url", lambda: server_url)

    from agent import main as agent_main
    threading.Thread(target=agent_main.run, args=(server_url,), daemon=True).start()
    time.sleep(0.8)  # give agent time to connect

    import requests
    s = requests.Session(); s.post(f"{server_url}/login", data={"password": "pw"})
    cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())
    browser = simple_websocket.Client(f"ws://127.0.0.1:{port}/agent/ws",
                                      headers={"Cookie": cookie})
    # Read presence messages until we see online=True (tolerate ordering races).
    for _ in range(5):
        first = json.loads(browser.receive(timeout=5))
        if first.get("type") == "presence" and first["payload"]["online"] is True:
            break
    else:
        pytest.fail("agent never reported presence=online within 5 messages")
    browser.send(json.dumps({"v": 1, "type": "ping", "payload": {"n": 42}}))
    pong = json.loads(browser.receive(timeout=5))
    assert pong["type"] == "pong" and pong["payload"]["n"] == 42
    browser.close()
