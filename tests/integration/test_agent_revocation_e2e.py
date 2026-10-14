"""End-to-end: a revoked agent must notice and re-pair, whatever the timing.

The relay accepts the upgrade even for a bad token, then sends
``{"type":"error","payload":{"reason":"unauthorized"}}`` and closes. That
frame is the agent's only revocation signal (``agent.main._on_message``
clears the token and drops to the re-pair prompt). Before the fix the agent
lost it whenever the close won the race with its hello, or when 101 + error
+ close arrived in one read, and then reconnected forever instead.
"""
import threading
import time

import pytest

simple_websocket = pytest.importorskip("simple_websocket")


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    import importlib
    import core.db as db, core.devices as devices
    importlib.reload(db); importlib.reload(devices); db.init_db()
    import app as m; importlib.reload(m)
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, m.app, threaded=True)
    srv.daemon_threads = True
    port = srv.socket.getsockname()[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _delay_first_handshake_recv(monkeypatch):
    """Make the client's first handshake recv() late enough that the 101,
    the error frame and the close all land in it."""
    from simple_websocket import ws as _ws
    orig = _ws.Client.handshake

    class _SlowFirstRecv:
        def __init__(self, sock):
            self._sock, self._first = sock, True

        def recv(self, n):
            if self._first:
                self._first = False
                time.sleep(0.5)
            return self._sock.recv(n)

        def __getattr__(self, name):
            return getattr(self._sock, name)

    def _slow_handshake(self):
        real = self.sock
        self.sock = _SlowFirstRecv(real)
        try:
            return orig(self)
        finally:
            self.sock = real
    monkeypatch.setattr(_ws.Client, "handshake", _slow_handshake)


@pytest.mark.parametrize("timing", [
    "natural",
    "close_before_hello",   # relay's close processed before the hello is sent
    "coalesced_with_101",   # 101 + error + close in one read (proxy-style)
])
def test_revoked_agent_clears_token_and_stops(live, monkeypatch, timing):
    from agent import config, transport
    from agent import main as agent_main

    # Fresh module-level events so this test can't leak a set flag.
    monkeypatch.setattr(agent_main, "_shutdown_event", threading.Event())
    monkeypatch.setattr(agent_main, "_token_revoked_event", threading.Event())
    monkeypatch.setattr(agent_main, "_RECONNECT_BASE_DELAY", 0.1)
    monkeypatch.setattr(agent_main, "_RECONNECT_MAX_DELAY", 0.2)
    cleared = threading.Event()
    monkeypatch.setattr(config, "clear_token", cleared.set)  # not the keyring
    pairings = []

    def _ensure_paired(_url):
        pairings.append(1)
        if len(pairings) == 1:
            return "token-the-server-does-not-know"
        raise SystemExit("")  # the re-pair prompt: stop here
    monkeypatch.setattr(agent_main, "_ensure_paired", _ensure_paired)

    connects = []
    real_connect = transport._connect

    def _connect(url):
        connects.append(1)
        ws = real_connect(url)
        if timing == "close_before_hello":
            deadline = time.monotonic() + 3
            while ws.connected and time.monotonic() < deadline:
                time.sleep(0.01)
        return ws
    monkeypatch.setattr(transport, "_connect", _connect)
    if timing == "coalesced_with_101":
        _delay_first_handshake_recv(monkeypatch)

    t = threading.Thread(target=agent_main.run, args=(live,), daemon=True)
    t.start()
    try:
        assert cleared.wait(8), (
            f"revoked token never cleared after {len(connects)} connect(s) — "
            f"the relay's 'unauthorized' frame was lost")
        t.join(5)
        assert not t.is_alive(), "run() should stop at the re-pair prompt"
        assert len(connects) == 1, f"reconnected {len(connects) - 1} time(s)"
    finally:
        agent_main._shutdown_event.set()
        t.join(5)
