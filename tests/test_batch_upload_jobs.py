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


def test_conc002_no_duplicate_history_row_under_concurrent_record(temp_db, monkeypatch):
    """CONC-002: the skip_set is built once at batch start. If a concurrent
    run records the same (session, date, platform) success between that build
    and this run's record_upload, the second write must be skipped so the
    History view doesn't gain a duplicate success row."""
    iso = "2025-05-21"

    def fake_yt(entry, **k):
        # Simulate a concurrent run finishing this exact row first — after
        # this run already passed the batch-start skip check.
        _db.record_upload(session_id="sess1", iso_date=iso, platform="YouTube Video",
                          title="t", file_path="", success=True,
                          url="https://yt/concurrent", scheduled_time="", error="")
        return {"success": True, "url": "https://yt/thisrun"}

    monkeypatch.setattr(upload_jobs, "yt_upload_video", fake_yt)
    entries = {iso: _entry(iso)}
    summary = [{"date": iso, "iso_date": iso, "platform": "YouTube Video", "title": "t"}]
    file_paths = {("youtube_video", iso): "/tmp/run/v"}
    events, emit = _collect_emit()
    upload_jobs.run_batch(dates=[iso], summary=summary, file_paths=file_paths,
                          session_id="sess1", emit=emit, entries_snapshot=entries)
    rows = [r for r in _db.get_history(session_id="sess1")
            if r["platform"] == "YouTube Video" and r["success"]]
    assert len(rows) == 1, f"duplicate upload_history row written: {len(rows)} rows"


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


def test_no_deadlock_when_email_rows_precede_youtube(temp_db, monkeypatch):
    """TC-COV-10 / CONC-003: with max_workers <= the number of Rock Email
    rows, the pool must not deadlock. Each email row blocks in
    _resolve_youtube_watch_url waiting on its date's YouTube result; if the
    email waiters fill every worker slot while the YouTube rows sit queued
    behind them, the batch hangs forever. This summary orders the email rows
    FIRST (worst case) with only 2 workers — it deadlocks unless email rows
    are submitted last."""
    import threading
    import time

    def fake_yt(entry, **k):
        time.sleep(0.05)
        return {"success": True, "url": "https://youtu.be/" + entry.date}

    def fake_email(entry, youtube_watch_url="", **k):
        return {"success": True, "url": "https://rock/email"}

    monkeypatch.setattr(upload_jobs, "yt_upload_video", fake_yt)
    monkeypatch.setattr(upload_jobs, "rock_schedule_email", fake_email)

    dates = ["2025-05-21", "2025-05-22"]
    entries = {d: _entry(d) for d in dates}
    # Email rows BEFORE their YouTube rows — the ordering that deadlocks the
    # naive single-pool submission with max_workers == #email rows.
    summary = (
        [{"date": d, "iso_date": d, "platform": "Rock Email", "title": "t"} for d in dates]
        + [{"date": d, "iso_date": d, "platform": "YouTube Video", "title": "t"} for d in dates]
    )
    file_paths = {("youtube_video", d): f"/tmp/run/v-{d}" for d in dates}

    finished = threading.Event()

    def _run():
        events, emit = _collect_emit()
        upload_jobs.run_batch(
            dates=dates, summary=summary, file_paths=file_paths,
            session_id="sess1", emit=emit, entries_snapshot=entries,
            config={"upload": {"max_workers": 2}},
        )
        finished.set()

    threading.Thread(target=_run, daemon=True).start()
    assert finished.wait(timeout=20), (
        "run_batch deadlocked: email waiters filled the pool while the "
        "YouTube rows they wait on sat queued"
    )


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
