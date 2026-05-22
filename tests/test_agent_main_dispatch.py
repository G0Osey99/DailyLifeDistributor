from agent import main as agent_main


class _FakeConn:
    def __init__(self): self.sent = []
    def send(self, message): self.sent.append(message)


def test_ping_still_answered_with_pong():
    conn = _FakeConn()
    agent_main._on_message(conn, {"v": 1, "type": "ping", "payload": {"n": 1}})
    assert conn.sent == [{"v": 1, "type": "pong", "payload": {"n": 1}}]


def test_scan_request_answered_with_scan_result(monkeypatch):
    conn = _FakeConn()
    fake_report = {"by_date": {"2026-01-15": {"video": ["a.mp4"]}},
                   "dates": ["2026-01-15"], "errors": {}}
    monkeypatch.setattr(agent_main.config, "get_media_roots", lambda: {"video": "/x"})
    monkeypatch.setattr(agent_main.scan, "scan_roots", lambda roots: fake_report)

    agent_main._on_message(conn, {"v": 1, "type": "scan_request", "payload": {}})

    assert conn.sent == [{"v": 1, "type": "scan_result", "payload": fake_report}]


def test_unknown_type_ignored():
    conn = _FakeConn()
    agent_main._on_message(conn, {"v": 1, "type": "whatever", "payload": {}})
    assert conn.sent == []
