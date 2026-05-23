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
    monkeypatch.setattr(config, "set_server_url", lambda u: captured.setdefault("saved_url", u))

    ok = pair.redeem("https://autoalert.pro", "CODE123", "Mac")
    assert ok is True
    assert captured["url"] == "https://autoalert.pro/agent/pair/redeem"
    assert captured["json"] == {"code": "CODE123", "name": "Mac"}
    assert captured["token"] == "tok-xyz"
    assert captured["saved_url"] == "https://autoalert.pro"  # server URL persisted


def test_redeem_failure_returns_false(monkeypatch):
    class _Resp:
        status_code = 400
        def json(self): return {"error": "invalid or expired code"}

    monkeypatch.setattr(pair.requests, "post", lambda *a, **k: _Resp())
    assert pair.redeem("https://x", "BAD", "Mac") is False


def test_redeem_includes_hwid_and_hostname_when_supplied(monkeypatch):
    """When hwid_hash/hostname kwargs are provided they must appear
    in the JSON body sent to /agent/pair/redeem."""
    captured = {}

    class _Resp:
        status_code = 200
        def json(self): return {"device_id": "d1", "token": "tok"}

    def fake_post(url, json, timeout):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(pair.requests, "post", fake_post)
    monkeypatch.setattr(config, "set_token", lambda t: None)
    monkeypatch.setattr(config, "set_server_url", lambda u: None)

    ok = pair.redeem(
        "https://x", "CODE", "Mac",
        hwid_hash="a" * 64,
        hostname="Studio",
    )
    assert ok is True
    assert captured["json"]["hwid_hash"] == "a" * 64
    assert captured["json"]["hostname"] == "Studio"
    assert captured["json"]["code"] == "CODE"
    assert captured["json"]["name"] == "Mac"


def test_redeem_omits_hwid_and_hostname_when_not_supplied(monkeypatch):
    """Backward-compat: tests/code that doesn't pass the new kwargs
    sends only the original {code, name} body."""
    captured = {}

    class _Resp:
        status_code = 200
        def json(self): return {"device_id": "d1", "token": "tok"}

    def fake_post(url, json, timeout):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(pair.requests, "post", fake_post)
    monkeypatch.setattr(config, "set_token", lambda t: None)
    monkeypatch.setattr(config, "set_server_url", lambda u: None)

    pair.redeem("https://x", "CODE", "Mac")
    assert "hwid_hash" not in captured["json"]
    assert "hostname" not in captured["json"]
