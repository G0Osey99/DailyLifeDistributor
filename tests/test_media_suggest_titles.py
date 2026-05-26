"""/media/suggest-titles: transcript -> LLM suggestions for the customize step."""
import pytest

from core import auth, media_session as ms


@pytest.fixture()
def client(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    auth.reset_lockouts()
    auth.set_password("pw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        c.post("/login", data={"password": "pw"})
        yield c


def test_suggest_titles_returns_suggestions(client, monkeypatch):
    import core.llm_title_gen as llm
    monkeypatch.setattr(llm, "is_llamafile_running", lambda: True)
    captured = {}

    def fake_gen(text, num_suggestions=5):
        captured["text"] = text
        captured["n"] = num_suggestions
        return ["Title A", "Title B"]

    monkeypatch.setattr(llm, "generate_title_suggestions", fake_gen)
    resp = client.post("/media/suggest-titles", json={"transcript": "we talk gratitude", "count": 2})
    assert resp.status_code == 200
    assert resp.get_json()["suggestions"] == ["Title A", "Title B"]
    assert captured["text"] == "we talk gratitude"
    assert captured["n"] == 2


def test_suggest_titles_empty_transcript_422(client):
    resp = client.post("/media/suggest-titles", json={"transcript": "  "})
    assert resp.status_code == 422


def test_suggest_titles_llm_down_503(client, monkeypatch):
    import core.llm_title_gen as llm
    monkeypatch.setattr(llm, "is_llamafile_running", lambda: False)
    resp = client.post("/media/suggest-titles", json={"transcript": "x"})
    assert resp.status_code == 503


def test_suggest_titles_omit_count_falls_through_to_config(client, monkeypatch):
    """When the client omits `count`, the server must pass
    ``num_suggestions=None`` to ``generate_title_suggestions`` so the
    function falls through to ``config.yaml`` (``llm.num_title_suggestions``).
    Previously the endpoint hardcoded 5 when count was absent, ignoring
    the operator's config."""
    import core.llm_title_gen as llm
    monkeypatch.setattr(llm, "is_llamafile_running", lambda: True)
    captured = {}

    def fake_gen(text, num_suggestions=5):
        captured["n"] = num_suggestions
        return ["A", "B", "C"]

    monkeypatch.setattr(llm, "generate_title_suggestions", fake_gen)
    resp = client.post("/media/suggest-titles", json={"transcript": "x"})
    assert resp.status_code == 200
    assert captured["n"] is None, \
        "endpoint must pass None when client omits count, so config wins"


def test_suggest_titles_explicit_count_still_honored(client, monkeypatch):
    """Backward compat: a client that DOES send count gets that exact
    value (clamped 1..10)."""
    import core.llm_title_gen as llm
    monkeypatch.setattr(llm, "is_llamafile_running", lambda: True)
    captured = {}

    def fake_gen(text, num_suggestions=5):
        captured["n"] = num_suggestions
        return ["A"]

    monkeypatch.setattr(llm, "generate_title_suggestions", fake_gen)
    resp = client.post("/media/suggest-titles", json={"transcript": "x", "count": 7})
    assert resp.status_code == 200
    assert captured["n"] == 7


def test_suggest_titles_garbage_count_falls_back_to_config(client, monkeypatch):
    """A non-integer ``count`` (rare but possible from a buggy client)
    is treated as "not specified" so the config still wins instead of
    being silently overridden by a sentinel."""
    import core.llm_title_gen as llm
    monkeypatch.setattr(llm, "is_llamafile_running", lambda: True)
    captured = {}

    def fake_gen(text, num_suggestions=5):
        captured["n"] = num_suggestions
        return ["A"]

    monkeypatch.setattr(llm, "generate_title_suggestions", fake_gen)
    resp = client.post("/media/suggest-titles", json={"transcript": "x", "count": "fish"})
    assert resp.status_code == 200
    assert captured["n"] is None
