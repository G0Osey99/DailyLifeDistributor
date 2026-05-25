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
    # Request-handler threads default to daemon_threads=False on
    # ThreadingMixIn. If a websocket stays connected at teardown,
    # those non-daemon threads keep the Python interpreter alive
    # forever — that's what hung CI run 26361639941 for >1h after
    # this test failed. Daemon threads die with the process.
    srv.daemon_threads = True
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

    # Poll the relay directly instead of sleeping a fixed 0.8s. On CI under
    # load (run 26361639941) the agent hadn't finished its websocket
    # handshake by the time the browser opened its socket, so no presence
    # frame was ever in the queue and receive() timed out → None →
    # json.loads(None) → TypeError. Wait up to 10s for the agent to land.
    from core import relay as _relay
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if _relay.online_agent_count() >= 1:
            break
        time.sleep(0.05)
    else:
        pytest.fail("agent never connected to relay within 10s")

    import requests
    s = requests.Session(); s.post(f"{server_url}/login", data={"password": "pw"})
    cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())
    browser = simple_websocket.Client(f"ws://127.0.0.1:{port}/agent/ws",
                                      headers={"Cookie": cookie})
    try:
        # Read presence messages until we see online=True (tolerate
        # ordering races). receive() returns None on timeout; guard against
        # it so a slow CI doesn't produce a confusing TypeError.
        for _ in range(5):
            raw = browser.receive(timeout=5)
            if raw is None:
                pytest.fail("relay sent no presence frame within 5s")
            first = json.loads(raw)
            # If the legacy login POST didn't authenticate the session,
            # the relay's /agent/ws handler answers with an "error" frame
            # then closes — so the subsequent receive() returns None and
            # the test would otherwise fail 5s later with the misleading
            # "no presence frame" message. Surface the auth rejection
            # directly so future flakes are instantly diagnosable.
            if first.get("type") == "error":
                reason = (first.get("payload") or {}).get("reason", "?")
                pytest.fail(
                    f"relay rejected the browser WebSocket with reason="
                    f"{reason!r} — POST /login likely didn't set the "
                    f"`authenticated` session marker (or LEGACY_PASSWORD_"
                    f"ENABLED wasn't read at request time). Cookies in "
                    f"jar at WS open: {s.cookies.get_dict()}"
                )
            if first.get("type") == "presence" and first["payload"]["online"] is True:
                break
        else:
            pytest.fail("agent never reported presence=online within 5 messages")
        browser.send(json.dumps({"v": 1, "type": "ping", "payload": {"n": 42}}))
        raw = browser.receive(timeout=5)
        assert raw is not None, "no pong frame received within 5s"
        pong = json.loads(raw)
        assert pong["type"] == "pong" and pong["payload"]["n"] == 42
    finally:
        browser.close()
