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
    monkeypatch.setattr(transport, "_connect",
                        lambda url: captured.setdefault("url", url) or _FakeWS([]))
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
