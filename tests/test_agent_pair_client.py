from agent import pair, config


def test_redeem_stores_token(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        def json(self): return {"device_id": "d1", "token": "tok-xyz"}

    def fake_post(url, json, timeout):
        captured["url"] = url; captured["json"] = json
        return _Resp()

    monkeypatch.setattr(pair.requests, "post", fake_post)
    monkeypatch.setattr(config, "set_token", lambda t: captured.setdefault("token", t))
    monkeypatch.setattr(config, "set_server_url", lambda u: None)

    ok = pair.redeem("https://autoalert.pro", "CODE123", "Mac")
    assert ok is True
    assert captured["url"] == "https://autoalert.pro/agent/pair/redeem"
    assert captured["json"] == {"code": "CODE123", "name": "Mac"}
    assert captured["token"] == "tok-xyz"


def test_redeem_failure_returns_false(monkeypatch):
    class _Resp:
        status_code = 400
        def json(self): return {"error": "invalid or expired code"}

    monkeypatch.setattr(pair.requests, "post", lambda *a, **k: _Resp())
    assert pair.redeem("https://x", "BAD", "Mac") is False
