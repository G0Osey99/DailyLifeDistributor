"""The title-suggestion request honors the `llm` config knobs."""
import core.llm_title_gen as m


class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": '["Title A", "Title B"]'}}]}


def test_request_uses_config_temperature_tokens_and_timeout(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(m, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(m.requests, "post", fake_post)
    monkeypatch.setattr(m, "_load_config", lambda: {"llm": {
        "temperature": 0.2,
        "max_tokens": 99,
        "request_timeout_seconds": 7,
        "num_title_suggestions": 2,
    }})

    titles = m.generate_title_suggestions("a unique transcript for the knob test")

    assert titles == ["Title A", "Title B"]
    assert captured["timeout"] == 7
    assert captured["json"]["temperature"] == 0.2
    assert captured["json"]["max_tokens"] == 99


def test_defaults_apply_when_llm_config_absent(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(m, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(m.requests, "post", fake_post)
    monkeypatch.setattr(m, "_load_config", lambda: {})  # no llm section

    m.generate_title_suggestions("another unique transcript without config")

    assert captured["timeout"] == m._LLM_REQUEST_TIMEOUT
    assert captured["json"]["temperature"] == m._LLM_TEMPERATURE
    assert captured["json"]["max_tokens"] == m._LLM_MAX_TOKENS
