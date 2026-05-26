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


def test_connect_arms_ping_interval(monkeypatch):
    """Without an application-level WebSocket keepalive, idle
    connections were dropped after ~24s by consumer-router NAT timers
    (Windows agent reconnect storm reported in the field). simple-
    websocket's ``ping_interval`` makes the protocol emit RFC 6455
    PING frames automatically; flask-sock auto-responds with PONG.
    The forwarded value must be set and less than the observed NAT
    window."""
    captured = {}

    def _fake_client(url, ssl_context=None, ping_interval=None):
        captured["ping_interval"] = ping_interval
        return _FakeWS([])

    import simple_websocket
    monkeypatch.setattr(simple_websocket, "Client", _fake_client)
    transport._connect("wss://autoalert.pro/agent/socket?token=x")
    assert captured["ping_interval"] == transport._PING_INTERVAL_S
    # Sanity: comfortably under the ~24s NAT window AND well under
    # Cloudflare's 100s WebSocket idle cap.
    assert 1.0 < transport._PING_INTERVAL_S < 30.0


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
