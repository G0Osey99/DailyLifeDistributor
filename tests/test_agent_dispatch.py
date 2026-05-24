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
    # youtube.client_secrets is platform-shared; store it via set_platform_secret.
    secrets_store.set_secret("youtube.token", '{"t":1}')
    secrets_store.set_platform_secret("youtube.client_secrets", '{"c":1}')
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


# ---------------------------------------------------------------------------
# Phase 3.5 — _pick_device fallback chain
# ---------------------------------------------------------------------------

def _make_devices_online(monkeypatch, agents):
    """Helper: patch _relay_online_agents to return *agents* (list of dicts)."""
    from core import agent_dispatch as ad
    monkeypatch.setattr(ad, "_relay_online_agents", lambda: list(agents))


def _seed_device(devices_mod, device_id, *, name="Mac",
                  hwid=None, hostname=None, last_seen_offset_s=0):
    """Create a row in agent_devices keyed by *device_id* with last_seen_at
    set to (now - last_seen_offset_s) so most_recently_seen_online() finds it.

    last_seen_offset_s defaults to 0 (NOW). A larger value back-dates the
    row by that many seconds — useful for ordering between rows. Stays
    within the default freshness window (60s) unless tests pass big values.
    """
    from datetime import datetime, timezone
    from core import db
    ts = datetime.now(timezone.utc).timestamp() - last_seen_offset_s
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    with db._get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_devices (id, name, token_hash, created_at, "
            "last_seen_at, revoked, hwid_hash, hostname) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (device_id, name, "hashval",
             iso, iso, hwid, hostname),
        )
        conn.commit()


def test_pick_device_explicit_id_wins_when_online(monkeypatch, temp_db):
    from core import agent_dispatch as ad, devices as _devices
    _seed_device(_devices, "dev-explicit", name="Mac")
    _seed_device(_devices, "dev-other", name="Studio")
    _make_devices_online(monkeypatch, [
        {"device_id": "dev-explicit", "device_name": "Mac", "connect_ip": "1.1.1.1"},
        {"device_id": "dev-other", "device_name": "Studio", "connect_ip": "2.2.2.2"},
    ])

    result = ad._pick_device(device_id="dev-explicit")
    assert result["id"] == "dev-explicit"


def test_pick_device_explicit_id_ignored_when_offline(monkeypatch, temp_db):
    """A specified but offline device falls through to the next strategy."""
    from core import agent_dispatch as ad, devices as _devices
    _seed_device(_devices, "dev-online", name="Studio")
    _make_devices_online(monkeypatch, [
        {"device_id": "dev-online", "device_name": "Studio", "connect_ip": "9.9.9.9"},
    ])

    # 'dev-explicit' is not online → step (1) skipped; len(online)==1 → step (2)
    # returns dev-online.
    result = ad._pick_device(device_id="dev-explicit-not-here")
    assert result["id"] == "dev-online"


def test_pick_device_single_online_auto_pick(monkeypatch, temp_db):
    from core import agent_dispatch as ad, devices as _devices
    _seed_device(_devices, "only-one", name="OnlyOne")
    _make_devices_online(monkeypatch, [
        {"device_id": "only-one", "device_name": "OnlyOne", "connect_ip": "1.1.1.1"},
    ])

    result = ad._pick_device()
    assert result["id"] == "only-one"


def test_pick_device_same_network_with_multiple_online(monkeypatch, temp_db):
    """Two online devices, only one shares browser IP → same-network wins."""
    from core import agent_dispatch as ad, devices as _devices
    _seed_device(_devices, "dev-far", name="Far")
    _seed_device(_devices, "dev-near", name="Near")
    _make_devices_online(monkeypatch, [
        {"device_id": "dev-far",  "device_name": "Far",  "connect_ip": "8.8.8.8"},
        {"device_id": "dev-near", "device_name": "Near", "connect_ip": "10.0.0.5"},
    ])

    result = ad._pick_device(browser_ip="10.0.0.5")
    assert result["id"] == "dev-near"


def test_pick_device_ambiguous_same_network_falls_through(monkeypatch, temp_db):
    """If two online devices share the browser IP, same-network is ambiguous
    and we fall through to most-recently-seen."""
    from core import agent_dispatch as ad, devices as _devices
    # dev-a is older (offset 30s back), dev-b is now → dev-b wins fallback.
    _seed_device(_devices, "dev-a", name="A", last_seen_offset_s=30)
    _seed_device(_devices, "dev-b", name="B", last_seen_offset_s=0)
    _make_devices_online(monkeypatch, [
        {"device_id": "dev-a", "device_name": "A", "connect_ip": "10.0.0.5"},
        {"device_id": "dev-b", "device_name": "B", "connect_ip": "10.0.0.5"},
    ])

    result = ad._pick_device(browser_ip="10.0.0.5")
    assert result["id"] == "dev-b"


def test_pick_device_most_recently_seen_fallback(monkeypatch, temp_db):
    """Two online, no browser_ip → step (3) skipped → step (4) most-recent."""
    from core import agent_dispatch as ad, devices as _devices
    _seed_device(_devices, "dev-old", name="Old", last_seen_offset_s=30)
    _seed_device(_devices, "dev-new", name="New", last_seen_offset_s=0)
    _make_devices_online(monkeypatch, [
        {"device_id": "dev-old", "device_name": "Old", "connect_ip": "1.1.1.1"},
        {"device_id": "dev-new", "device_name": "New", "connect_ip": "2.2.2.2"},
    ])

    result = ad._pick_device()  # no browser_ip
    assert result["id"] == "dev-new"


def test_pick_device_raises_when_no_agents(monkeypatch, temp_db):
    """No online + no recently-seen → NoAgentOnlineError."""
    from core import agent_dispatch as ad
    _make_devices_online(monkeypatch, [])
    import pytest
    with pytest.raises(ad.NoAgentOnlineError):
        ad._pick_device()


def test_pick_device_browser_ip_unknown_skips_same_network(monkeypatch, temp_db):
    """browser_ip='unknown' must NOT match an agent stored as connect_ip='unknown'
    (defensive: never collapse two missing signals into a match)."""
    from core import agent_dispatch as ad, devices as _devices
    _seed_device(_devices, "dev-a", name="A", last_seen_offset_s=30)
    _seed_device(_devices, "dev-b", name="B", last_seen_offset_s=0)
    _make_devices_online(monkeypatch, [
        {"device_id": "dev-a", "device_name": "A", "connect_ip": "unknown"},
        {"device_id": "dev-b", "device_name": "B", "connect_ip": "2.2.2.2"},
    ])

    # browser_ip='unknown' shouldn't match dev-a; falls through to most-recent.
    result = ad._pick_device(browser_ip="unknown")
    assert result["id"] == "dev-b"


def test_start_passes_device_id_to_pick(monkeypatch, temp_db):
    """start(device_id=...) flows through to _pick_device."""
    from core import agent_dispatch, secrets_store, relay
    secrets_store.set_secret("youtube.token", "{}")
    captured = {}

    def _fake_pick(device_id=None, browser_ip=None):
        captured["device_id"] = device_id
        captured["browser_ip"] = browser_ip
        return {"id": "dev-xyz", "name": "Mac"}

    monkeypatch.setattr(agent_dispatch, "_pick_device", _fake_pick)
    monkeypatch.setattr(relay, "send_to_device", lambda *a, **k: None)

    agent_dispatch.start(
        session_id="S1",
        summary=[{"date": "2026-05-22", "platforms": ["YouTube Video"]}],
        entries={"2026-05-22": _entry()},
        elements={"youtube_video_enabled": True},
        config={"max_workers": 4},
        device_id="dev-explicit",
        browser_ip="10.0.0.5",
    )
    assert captured["device_id"] == "dev-explicit"
    assert captured["browser_ip"] == "10.0.0.5"


def test_start_sends_envelope_through_relay_and_returns_job_id(monkeypatch, temp_db):
    from core import agent_dispatch, secrets_store, relay
    secrets_store.set_secret("youtube.token", "{}")
    sent: list = []
    monkeypatch.setattr(relay, "send_to_device",
                        lambda device_name, envelope: sent.append((device_name, envelope)))
    monkeypatch.setattr(agent_dispatch, "_pick_device",
                        lambda **kw: {"name": "mac-1", "id": "dev-1"})

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
