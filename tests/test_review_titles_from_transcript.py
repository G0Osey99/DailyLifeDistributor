"""Title suggestions come from the mapped transcript text — no Whisper."""
import importlib.util

import pytest

from core.session_state import session, ReviewEntry


@pytest.fixture(autouse=True)
def _clean_session():
    session.entries.clear()
    session.selected_dates.clear()
    yield
    session.entries.clear()
    session.selected_dates.clear()


def test_titles_use_entry_transcript(monkeypatch, temp_db):
    import app as flask_app_module
    app = flask_app_module.app
    from blueprints import review as review_mod

    entry = ReviewEntry(date="2025-05-21", display_date="May 21, 2025")
    entry.transcript = "Today we talk about gratitude."
    session.entries["2025-05-21"] = entry

    captured = {}

    def fake_gen(text, num_suggestions=5):
        captured["text"] = text
        return ["Gratitude Today", "Thankful Moments"]

    monkeypatch.setattr(review_mod, "generate_title_suggestions", fake_gen)
    monkeypatch.setattr(review_mod, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(review_mod, "clear_llm_cache", lambda: None)

    review_mod._set_title_job("job1", status="running")
    review_mod._run_title_generation("job1", "2025-05-21", app)

    job = review_mod._title_jobs["job1"]
    assert job["status"] == "done", job
    assert job["suggestions"] == ["Gratitude Today", "Thankful Moments"]
    assert captured["text"] == "Today we talk about gratitude."


def test_no_transcript_errors_without_transcription(monkeypatch, temp_db):
    import app as flask_app_module
    app = flask_app_module.app
    from blueprints import review as review_mod

    entry = ReviewEntry(date="2025-05-22", display_date="May 22, 2025")
    entry.transcript = ""
    session.entries["2025-05-22"] = entry

    monkeypatch.setattr(review_mod, "is_llamafile_running", lambda: True)
    monkeypatch.setattr(
        review_mod, "generate_title_suggestions",
        lambda *a, **k: pytest.fail("should not generate without a transcript"),
    )

    review_mod._set_title_job("job2", status="running")
    review_mod._run_title_generation("job2", "2025-05-22", app)
    job = review_mod._title_jobs["job2"]
    assert job["status"] == "error"


def test_transcriber_module_removed():
    assert importlib.util.find_spec("core.transcriber") is None
