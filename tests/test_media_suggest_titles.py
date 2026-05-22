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
