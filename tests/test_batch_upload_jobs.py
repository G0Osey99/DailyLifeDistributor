"""Batch runner: file dedup, idempotent skip, email-after-YouTube ordering."""
import pytest

from core import db as _db, upload_jobs
from core.session_state import ReviewEntry, session


@pytest.fixture(autouse=True)
def _clean_session():
    session.upload_results.clear()
    yield
    session.upload_results.clear()


def _entry(iso):
    return ReviewEntry(date=iso, display_date=iso)


def _collect_emit():
    events = []
    return events, events.append


def test_dedup_by_physical_file(temp_db, monkeypatch):
    calls = {"yt": 0, "sc": 0}
    monkeypatch.setattr(upload_jobs, "yt_upload_video",
                        lambda *a, **k: (calls.__setitem__("yt", calls["yt"] + 1) or
                                         {"success": True, "url": "https://yt/x"}))
    monkeypatch.setattr(upload_jobs, "sc_upload_episode",
                        lambda *a, **k: (calls.__setitem__("sc", calls["sc"] + 1) or
                                         {"success": True, "url": "https://sc/x"}))

    iso = "2025-05-21"
    entries = {iso: _entry(iso)}
    summary = [
        {"date": iso, "iso_date": iso, "platform": "YouTube Video", "title": "t"},
        {"date": iso, "iso_date": iso, "platform": "SimpleCast", "title": "t"},
    ]
    shared = "/tmp/run/file-abc"
    # Two categories pointing at the same physical file.
    file_paths = {("youtube_video", iso): shared, ("podcast", iso): shared}

    events, emit = _collect_emit()
    distinct = upload_jobs.run_batch(
        dates=[iso], summary=summary, file_paths=file_paths,
        session_id="sess1", emit=emit, entries_snapshot=entries,
    )
    # Both uploaders ran, but the shared physical file is counted once.
    assert calls["yt"] == 1 and calls["sc"] == 1
    assert distinct == {shared}


def test_idempotent_skip_already_succeeded(temp_db, monkeypatch):
    calls = {"yt": 0, "sc": 0}
    monkeypatch.setattr(upload_jobs, "yt_upload_video",
                        lambda *a, **k: (calls.__setitem__("yt", calls["yt"] + 1) or
                                         {"success": True, "url": "https://yt/x"}))
    monkeypatch.setattr(upload_jobs, "sc_upload_episode",
                        lambda *a, **k: (calls.__setitem__("sc", calls["sc"] + 1) or
                                         {"success": True, "url": "https://sc/x"}))

    iso = "2025-05-21"
    # Seed a prior success for the YouTube Video row in this session.
    _db.record_upload(session_id="sess1", iso_date=iso, platform="YouTube Video",
                      title="t", file_path="", success=True, url="https://yt/old",
                      scheduled_time="", error="")

    entries = {iso: _entry(iso)}
    summary = [
        {"date": iso, "iso_date": iso, "platform": "YouTube Video", "title": "t"},
        {"date": iso, "iso_date": iso, "platform": "SimpleCast", "title": "t"},
    ]
    file_paths = {("youtube_video", iso): "/tmp/run/v", ("podcast", iso): "/tmp/run/a"}

    events, emit = _collect_emit()
    upload_jobs.run_batch(dates=[iso], summary=summary, file_paths=file_paths,
                          session_id="sess1", emit=emit, entries_snapshot=entries)

    assert calls["yt"] == 0          # already succeeded → skipped, uploader not called
    assert calls["sc"] == 1          # the other platform still runs
    skipped = [e for e in events if e["type"] == "skip" and e["platform"] == "YouTube Video"]
    assert skipped


def test_email_waits_for_youtube_url(temp_db, monkeypatch):
    captured = {}

    def fake_yt(entry, **k):
        return {"success": True, "url": "https://youtu.be/WATCH"}

    def fake_email(entry, youtube_watch_url="", **k):
        captured["watch_url"] = youtube_watch_url
        return {"success": True, "url": "https://rock/email"}

    monkeypatch.setattr(upload_jobs, "yt_upload_video", fake_yt)
    monkeypatch.setattr(upload_jobs, "rock_schedule_email", fake_email)

    iso = "2025-05-21"
    entries = {iso: _entry(iso)}
    summary = [
        {"date": iso, "iso_date": iso, "platform": "YouTube Video", "title": "t"},
        {"date": iso, "iso_date": iso, "platform": "Rock Email", "title": "t"},
    ]
    file_paths = {("youtube_video", iso): "/tmp/run/v"}

    events, emit = _collect_emit()
    upload_jobs.run_batch(dates=[iso], summary=summary, file_paths=file_paths,
                          session_id="sess1", emit=emit, entries_snapshot=entries)

    assert captured.get("watch_url") == "https://youtu.be/WATCH"


def test_paths_pointed_at_temp_files(temp_db, monkeypatch):
    seen = {}
    monkeypatch.setattr(upload_jobs, "yt_upload_video",
                        lambda entry, **k: seen.__setitem__("video", entry.youtube_video_path) or
                        {"success": True, "url": "u"})
    iso = "2025-05-21"
    entries = {iso: _entry(iso)}
    summary = [{"date": iso, "iso_date": iso, "platform": "YouTube Video", "title": "t"}]
    file_paths = {("youtube_video", iso): "/tmp/run/the-temp-file"}

    events, emit = _collect_emit()
    upload_jobs.run_batch(dates=[iso], summary=summary, file_paths=file_paths,
                          session_id="sess1", emit=emit, entries_snapshot=entries)
    assert seen["video"] == "/tmp/run/the-temp-file"
