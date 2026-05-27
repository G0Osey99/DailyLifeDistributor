import json
from agent import transport


class _FakeWS:
    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []
        self.closed = False
    def send(self, text): self.sent.append(text)
    def receive(self, timeout=None):
        return self._incoming.pop(0) if self._incoming else None
    def close(self): self.closed = True


def test_handshake_sends_hello(monkeypatch):
    fake = _FakeWS(incoming=[])
    monkeypatch.setattr(transport, "_connect", lambda url: fake)
    conn = transport.AgentConnection("https://autoalert.pro", "tok")
    conn.connect()
    sent = json.loads(fake.sent[0])
    assert sent["type"] == "hello" and sent["v"] == transport.PROTOCOL_VERSION


def test_url_uses_wss_and_token(monkeypatch):
    captured = {}

    def _cap(url):
        captured["url"] = url
        return _FakeWS([])

    monkeypatch.setattr(transport, "_connect", _cap)
    transport.AgentConnection("https://autoalert.pro", "tok-9").connect()
    assert captured["url"] == "wss://autoalert.pro/agent/socket?token=tok-9"


def test_run_handles_ping_with_handler(monkeypatch):
    fake = _FakeWS(incoming=[json.dumps({"v": 1, "type": "ping", "payload": {"x": 7}})])
    monkeypatch.setattr(transport, "_connect", lambda url: fake)
    conn = transport.AgentConnection("https://x", "t")
    conn.connect()
    seen = []
    conn.run_once(lambda msg: seen.append(msg))
    assert seen == [{"v": 1, "type": "ping", "payload": {"x": 7}}]


# ── certifi / TLS context (v0.7.1) ──────────────────────────────────


def test_build_ssl_context_uses_certifi_bundle():
    """The wss SSL context must load certifi's cacert.pem, not whatever
    the host system OpenSSL was built against. PyInstaller .app bundles
    that fall through to the system trust store end up with no trust
    anchors, every wss handshake hangs forever, and the GUI sits at
    "Connecting…" with no log evidence (the bug surfaced in v0.7.0)."""
    import certifi
    ctx = transport._build_ssl_context()
    # Smoke check: the context can resolve real CA chains. The cheapest
    # proof is "the context has trust anchors loaded" — get_ca_certs()
    # returns an empty list when the cafile was missing or unreadable.
    assert ctx.get_ca_certs(), "SSL context loaded zero CAs from certifi"
    # And the cert file we asked for actually exists.
    import os
    assert os.path.isfile(certifi.where()), \
        "certifi.where() returned a non-existent path"


def test_connect_passes_certifi_ssl_context_for_wss(monkeypatch):
    """_connect must forward an ssl_context to simple_websocket.Client
    for wss URLs so the bundled certifi CAs are used."""
    captured = {}

    def _fake_client(url, ssl_context=None, **_kw):
        captured["url"] = url
        captured["ssl_context"] = ssl_context
        return _FakeWS([])

    import simple_websocket
    monkeypatch.setattr(simple_websocket, "Client", _fake_client)
    transport._connect("wss://autoalert.pro/agent/socket?token=tok-9")
    assert captured["ssl_context"] is not None, \
        "wss connect must build an explicit SSL context"
    # Sanity: same CAs as the certifi bundle.
    assert captured["ssl_context"].get_ca_certs(), \
        "forwarded SSL context had no trust anchors"


def test_connect_skips_ssl_context_for_ws(monkeypatch):
    """Plain ws:// (used in tests / local dev) must NOT build an SSL
    context — wrapping a plain TCP socket in TLS would just fail."""
    captured = {}

    def _fake_client(url, ssl_context=None, **_kw):
        captured["ssl_context"] = ssl_context
        return _FakeWS([])

    import simple_websocket
    monkeypatch.setattr(simple_websocket, "Client", _fake_client)
    transport._connect("ws://localhost:5000/agent/socket?token=x")
    assert captured["ssl_context"] is None


def test_connect_does_not_pass_library_ping_interval(monkeypatch):
    """simple_websocket's built-in ``ping_interval`` spawns a background
    thread that calls ``ws.send()`` + ``sock.send()`` concurrently with
    user-thread sends. Neither wsproto's connection state nor the SSL
    socket is thread-safe — concurrent writes in the field surfaced as
    ``[SSL] internal error (_ssl.c:2427)`` and dropped the WebSocket
    mid-job. We rely on a single-threaded app-level keepalive in
    ``AgentConnection.run_once`` instead. This test guards the contract
    so a future "fix" doesn't reintroduce the library ping."""
    captured = {}

    def _fake_client(url, ssl_context=None, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeWS([])

    import simple_websocket
    monkeypatch.setattr(simple_websocket, "Client", _fake_client)
    transport._connect("wss://autoalert.pro/agent/socket?token=x")
    assert "ping_interval" not in captured["kwargs"], (
        "must not pass ping_interval — library's ping thread is "
        "not thread-safe with user-thread sends"
    )
    # Sanity: app-level keepalive cadence still under Cloudflare's 100s cap.
    assert 1.0 < transport._PING_INTERVAL_S < 30.0


def test_send_serializes_through_lock(monkeypatch):
    """All outbound frames must go through ``_send_lock`` so worker-thread
    event emission can't race the receive-thread keepalive. Without this
    serialization, simple_websocket's non-thread-safe internals corrupt
    the SSL stream and the field bug returns."""
    import threading
    fake = _FakeWS([])
    monkeypatch.setattr(transport, "_connect", lambda url: fake)
    conn = transport.AgentConnection("https://x", "tok")
    conn.connect()  # hello frame already on the wire

    # 50 concurrent senders — without a lock the fake's `sent` list
    # would still be intact but the underlying wsproto/sock calls
    # would interleave. We can't observe SSL corruption in a fake,
    # but we CAN verify the lock attribute exists and is acquired
    # when send() runs.
    barrier = threading.Barrier(50)
    acquired_under_lock = []

    real_send = conn.ws.send  # capture the fake's send

    def _checking_send(payload):
        # When ``conn.send`` is in progress, ``_send_lock`` must be held.
        acquired_under_lock.append(conn._send_lock.locked())
        return real_send(payload)

    conn.ws.send = _checking_send

    def worker():
        barrier.wait()
        conn.send({"v": 1, "type": "event", "payload": {}})

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert acquired_under_lock, "no sends were observed"
    # Every observed send happened with the lock held.
    assert all(acquired_under_lock), (
        f"send executed without holding _send_lock "
        f"({acquired_under_lock.count(False)} of {len(acquired_under_lock)} runs)"
    )


def test_run_once_emits_keepalive_when_idle(monkeypatch):
    """When the WebSocket has been idle for ``_PING_INTERVAL_S``, the
    receive loop must emit an app-level keepalive frame to keep
    consumer-router NAT mappings alive (the original purpose of the
    library's ping_interval, now done safely on the single receive
    thread)."""
    import time as _time
    fake = _FakeWS([])
    monkeypatch.setattr(transport, "_connect", lambda url: fake)
    conn = transport.AgentConnection("https://x", "tok")
    conn.connect()
    # Burn through any sends from connect() so the assertion below
    # only counts keepalives.
    fake.sent.clear()
    # Pretend the last send was long enough ago to trigger keepalive.
    conn._last_send_at = _time.monotonic() - (transport._PING_INTERVAL_S + 1)

    # run_once will receive None (timeout) then loop. Set shutdown after
    # one poll so the test doesn't block.
    def _stop_after_one_poll(*a, **kw):
        conn._shutdown.set()
        return None
    fake.receive = _stop_after_one_poll

    conn.run_once(lambda m: None)

    # At least one keepalive frame was emitted.
    assert any('"keepalive"' in s for s in fake.sent), (
        f"expected a keepalive frame in fake.sent; got: {fake.sent}"
    )


def test_connect_bounds_handshake_with_socket_default_timeout(monkeypatch):
    """If simple_websocket's Client() hangs (e.g. TLS handshake stalls),
    a bounded socket.setdefaulttimeout prevents an infinite hang. The
    previous behavior had no timeout and the GUI sat at "Connecting…"
    forever with no log."""
    import socket as _socket
    seen = {}

    def _fake_client(url, ssl_context=None, **_kw):
        seen["timeout_during_call"] = _socket.getdefaulttimeout()
        return _FakeWS([])

    import simple_websocket
    monkeypatch.setattr(simple_websocket, "Client", _fake_client)
    before = _socket.getdefaulttimeout()
    transport._connect("wss://autoalert.pro/agent/socket?token=x")
    after = _socket.getdefaulttimeout()
    assert seen["timeout_during_call"] == transport._CONNECT_TIMEOUT_S, \
        "_connect must arm a socket default timeout for the Client() call"
    # Restored on the way out so other sockets (e.g. the sessions poll)
    # aren't affected.
    assert before == after, \
        "_connect must restore the previous socket default timeout"
