import sqlite3


def test_external_calendar_items_table_exists(temp_db):
    with sqlite3.connect(temp_db._DB_PATH) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='external_calendar_items'"
        ).fetchall()
    assert rows, "external_calendar_items table was not created"


def test_upload_history_has_external_id_column(temp_db):
    with sqlite3.connect(temp_db._DB_PATH) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info('upload_history')").fetchall()]
    assert "external_id" in cols


def _make_item(platform="youtube_video", external_id="vid1", iso_date="2026-05-10",
               title="t", url="u", status="scheduled", scheduled_time="2026-05-10T08:00:00",
               raw_json="{}"):
    return {
        "platform": platform, "external_id": external_id, "iso_date": iso_date,
        "title": title, "url": url, "status": status,
        "scheduled_time": scheduled_time, "raw_json": raw_json,
    }


def test_upsert_inserts_new_rows(temp_db):
    temp_db.upsert_external_items([_make_item()])
    rows = temp_db.get_external_items_for_window("2026-05-01", "2026-05-31")
    assert len(rows) == 1
    assert rows[0]["external_id"] == "vid1"
    assert rows[0]["status"] == "scheduled"


def test_upsert_updates_existing_row_in_place(temp_db):
    temp_db.upsert_external_items([_make_item(title="old")])
    temp_db.upsert_external_items([_make_item(title="new", status="published")])
    rows = temp_db.get_external_items_for_window("2026-05-01", "2026-05-31")
    assert len(rows) == 1
    assert rows[0]["title"] == "new"
    assert rows[0]["status"] == "published"


def test_window_filters_by_iso_date(temp_db):
    temp_db.upsert_external_items([
        _make_item(external_id="a", iso_date="2026-05-10"),
        _make_item(external_id="b", iso_date="2026-06-15"),
    ])
    rows = temp_db.get_external_items_for_window("2026-05-01", "2026-05-31")
    assert {r["external_id"] for r in rows} == {"a"}


def test_window_excludes_deleted_status(temp_db):
    temp_db.upsert_external_items([_make_item(external_id="a")])
    temp_db.mark_stale_external_items("youtube_video", "2026-05-01", "2026-05-31", seen_ids=set())
    rows = temp_db.get_external_items_for_window("2026-05-01", "2026-05-31")
    assert rows == []


def test_mark_stale_only_affects_named_platform(temp_db):
    temp_db.upsert_external_items([
        _make_item(platform="youtube_video", external_id="yt1"),
        _make_item(platform="rock", external_id="r1"),
    ])
    temp_db.mark_stale_external_items("youtube_video", "2026-05-01", "2026-05-31", seen_ids=set())
    rows = temp_db.get_external_items_for_window("2026-05-01", "2026-05-31")
    assert {r["external_id"] for r in rows} == {"r1"}


def test_mark_stale_keeps_seen_ids_alive(temp_db):
    temp_db.upsert_external_items([
        _make_item(external_id="keep"),
        _make_item(external_id="drop"),
    ])
    temp_db.mark_stale_external_items(
        "youtube_video", "2026-05-01", "2026-05-31", seen_ids={"keep"}
    )
    rows = temp_db.get_external_items_for_window("2026-05-01", "2026-05-31")
    assert {r["external_id"] for r in rows} == {"keep"}


def test_record_upload_stores_external_id(temp_db):
    temp_db.record_upload(
        session_id="s1", iso_date="2026-05-10", platform="youtube_video",
        title="t", file_path="/x", success=True,
        url="https://youtu.be/dQw4w9WgXcQ", scheduled_time="", error="",
    )
    rows = temp_db.get_history(limit=10)
    assert rows[0]["external_id"] == "dQw4w9WgXcQ"


def test_backfill_external_ids_populates_missing_rows(temp_db):
    with sqlite3.connect(temp_db._DB_PATH) as conn:
        conn.execute(
            "INSERT INTO upload_history (platform, url, success) VALUES (?, ?, 1)",
            ("simplecast",
             "https://dashboard.simplecast.com/accounts/a/shows/b/episodes/"
             "2f3f5d1c-aa24-4be4-b3c9-d12c9d88f3ad/"),
        )
        conn.commit()
    temp_db.backfill_external_ids()
    rows = temp_db.get_history(limit=10)
    assert rows[0]["external_id"] == "2f3f5d1c-aa24-4be4-b3c9-d12c9d88f3ad"


def test_backfill_skips_already_populated_rows(temp_db):
    """Backfill must not overwrite a non-NULL external_id."""
    with sqlite3.connect(temp_db._DB_PATH) as conn:
        conn.execute(
            "INSERT INTO upload_history (platform, url, external_id, success) VALUES (?, ?, ?, 1)",
            ("youtube_video", "https://youtu.be/aaa", "MANUAL_OVERRIDE"),
        )
        conn.commit()
    temp_db.backfill_external_ids()
    rows = temp_db.get_history(limit=10)
    assert rows[0]["external_id"] == "MANUAL_OVERRIDE"
