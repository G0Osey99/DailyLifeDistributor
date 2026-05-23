# tests/test_agent_dispatch.py
import json
from core import agent_dispatch
from core.session_state import ReviewEntry


def _entry(date="2026-05-22"):
    # ReviewEntry requires both `date` and `display_date` (no default for the latter).
    e = ReviewEntry(date=date, display_date="May 22, 2026")
    e.youtube_title = "T"
    e.media_path = "/server/tmp/v.mp4"   # not a real field — stays harmless
    e.thumbnail_path = "/server/tmp/th.png"
    return e


def test_build_envelope_strips_path_fields_from_entries():
    entries = {"2026-05-22": _entry()}
    elements = {"youtube_video_enabled": True}
    env = agent_dispatch.build_envelope(
        job_id="J1",
        rows=[{"row_idx": 0, "iso_date": "2026-05-22",
               "platforms": ["YouTube Video"], "elements": elements}],
        entries=entries,
        credentials={"youtube.token": "{}"},
        config={"max_workers": 4},
    )
    assert env["type"] == "job_plan"
    assert env["job_id"] == "J1"
    assert env["protocol_version"] == 1
    assert env["rows"][0]["entry"]["youtube_title"] == "T"
    # thumbnail_path is a real ReviewEntry field — must be stripped
    assert "thumbnail_path" not in env["rows"][0]["entry"]
    # youtube_video_path is another real path field — must be stripped
    assert "youtube_video_path" not in env["rows"][0]["entry"]
    assert env["credentials"] == {"youtube.token": "{}"}
    assert json.dumps(env)  # round-trips as JSON


def test_filter_already_done_rows_drops_completed_platforms(temp_db, monkeypatch):
    from core import agent_dispatch, db as _db
    _db.record_upload(
        session_id="S1", iso_date="2026-05-22", platform="YouTube Video",
        title="", file_path="", success=True, url="", scheduled_time="", error="",
    )
    summary = [
        {"date": "2026-05-22", "platforms": ["YouTube Video", "Rock"]},
        {"date": "2026-05-23", "platforms": ["YouTube Video"]},
    ]
    rows = agent_dispatch.filter_done_rows(session_id="S1", summary=summary)
    # YouTube Video on 05-22 is done — dropped. Rock on 05-22 + YouTube on 05-23 remain.
    assert rows == [
        {"row_idx": 0, "iso_date": "2026-05-22", "platforms": ["Rock"]},
        {"row_idx": 1, "iso_date": "2026-05-23", "platforms": ["YouTube Video"]},
    ]


def test_filter_drops_row_entirely_when_all_platforms_done(temp_db):
    from core import agent_dispatch, db as _db
    _db.record_upload(
        session_id="S1", iso_date="2026-05-22", platform="YouTube Video",
        title="", file_path="", success=True, url="", scheduled_time="", error="",
    )
    summary = [{"date": "2026-05-22", "platforms": ["YouTube Video"]}]
    assert agent_dispatch.filter_done_rows(session_id="S1", summary=summary) == []


def test_collect_credentials_pulls_needed_keys_only():
    from core import agent_dispatch, secrets_store
    # YouTube keys stored as kv secrets; session keys stored as blobs
    # (playwright_session stores them under "playwright.<basename_no_ext>").
    secrets_store.set_secret("youtube.token", '{"t":1}')
    secrets_store.set_secret("youtube.client_secrets", '{"c":1}')
    secrets_store.set_blob("playwright.rock_session", b'{"r":1}')
    secrets_store.set_blob("playwright.simplecast_session", b'{"s":1}')
    secrets_store.set_blob("playwright.vista_social_session", b'{"v":1}')
    creds = agent_dispatch.collect_credentials(
        platforms_in_use={"YouTube Video", "Rock"},
    )
    # Only the keys actually needed for selected platforms come through.
    assert set(creds.keys()) == {
        "youtube.token", "youtube.client_secrets", "playwright.rock_session",
    }
    assert creds["youtube.token"] == '{"t":1}'


def test_collect_credentials_omits_missing_keys():
    from core import agent_dispatch
    # Nothing in store.
    assert agent_dispatch.collect_credentials(platforms_in_use={"Rock"}) == {}


def test_start_sends_envelope_through_relay_and_returns_job_id(monkeypatch, temp_db):
    from core import agent_dispatch, secrets_store, relay
    secrets_store.set_secret("youtube.token", "{}")
    sent: list = []
    monkeypatch.setattr(relay, "send_to_device",
                        lambda device_name, envelope: sent.append((device_name, envelope)))
    monkeypatch.setattr(agent_dispatch, "_pick_device",
                        lambda: {"name": "mac-1", "id": "dev-1"})

    job_id = agent_dispatch.start(
        session_id="S1",
        summary=[{"date": "2026-05-22", "platforms": ["YouTube Video"]}],
        entries={"2026-05-22": _entry()},
        elements={"youtube_video_enabled": True},
        config={"max_workers": 4},
    )
    assert isinstance(job_id, str) and len(job_id) > 0
    assert len(sent) == 1
    device, env = sent[0]
    # Routing is by device_id (relay rooms key by id, not name).
    assert device == "dev-1"
    assert env["type"] == "job_plan"
    assert env["job_id"] == job_id
    assert env["rows"][0]["iso_date"] == "2026-05-22"
