"""record_upload + calendar refresh stamp org_id on every new row.

Before this fix, upload_history was scoped at read time but NULL-stamped
at write time, and external_calendar_items had no org_id column at all.
A calendar refresh while impersonating wrote NULL-org rows that then
surfaced under every tenant's calendar.
"""
from __future__ import annotations

import pytest

from core import calendar_refresh, db as _db, org_store


@pytest.fixture(autouse=True)
def _iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(_db)
    _db.init_db()
    yield


# ── record_upload ─────────────────────────────────────────────────────


def test_record_upload_explicit_org_id_stamped():
    _db.save_session("s1", "label", "{}", "in_progress", org_id=7)
    _db.record_upload(
        "s1", "2026-05-10", "YouTube Video", "Vid", "", True,
        "https://youtu.be/abc", "", "", org_id=7,
    )
    rows = _db.get_history(session_id="s1")
    assert rows[0]["org_id"] == 7


def test_record_upload_falls_back_to_session_org():
    """No explicit org_id → resolve from sessions.org_id."""
    _db.save_session("s2", "label", "{}", "in_progress", org_id=11)
    _db.record_upload(
        "s2", "2026-05-10", "YouTube Video", "Vid", "", True,
        "https://youtu.be/abc", "", "",
    )
    rows = _db.get_history(session_id="s2")
    assert rows[0]["org_id"] == 11


def test_record_upload_legacy_unstamped_session_stays_null():
    """Session row with NULL org_id → upload row NULL. Legacy / single-tenant."""
    _db.save_session("s3", "label", "{}", "in_progress", org_id=None)
    _db.record_upload(
        "s3", "2026-05-10", "YouTube Video", "Vid", "", True, "", "", "",
    )
    rows = _db.get_history(session_id="s3")
    assert rows[0]["org_id"] is None


# ── upsert_external_items / get_external_items_for_window ─────────────


def test_upsert_external_items_stamps_org_id():
    item = {
        "platform": "YouTube Video", "external_id": "yt-1",
        "iso_date": "2026-05-10", "scheduled_time": "10:00",
        "title": "Foo", "url": "https://youtu.be/yt-1",
        "status": "scheduled", "raw_json": "{}",
    }
    _db.upsert_external_items([item], org_id=42)
    rows = _db.get_external_items_for_window("2026-05-01", "2026-05-31", org_id=42)
    assert len(rows) == 1
    assert rows[0]["org_id"] == 42


def test_upsert_preserves_org_on_update_when_caller_omits():
    item = {
        "platform": "YouTube Video", "external_id": "yt-2",
        "iso_date": "2026-05-10", "scheduled_time": "", "title": "T", "url": "",
        "status": "scheduled", "raw_json": "{}",
    }
    _db.upsert_external_items([item], org_id=5)
    # Second upsert without org_id — must not clobber.
    _db.upsert_external_items([{**item, "title": "T2"}])
    rows = _db.get_external_items_for_window("2026-05-01", "2026-05-31", org_id=5)
    assert len(rows) == 1
    assert rows[0]["org_id"] == 5
    assert rows[0]["title"] == "T2"


def test_external_items_window_scopes_by_org():
    item = lambda eid, plat="YouTube Video": {
        "platform": plat, "external_id": eid,
        "iso_date": "2026-05-10", "scheduled_time": "10:00",
        "title": eid, "url": "", "status": "scheduled", "raw_json": "{}",
    }
    _db.upsert_external_items([item("a")], org_id=1)
    _db.upsert_external_items([item("b")], org_id=2)
    rows_1 = _db.get_external_items_for_window("2026-05-01", "2026-05-31", org_id=1)
    rows_2 = _db.get_external_items_for_window("2026-05-01", "2026-05-31", org_id=2)
    assert {r["external_id"] for r in rows_1} == {"a"}
    assert {r["external_id"] for r in rows_2} == {"b"}


def test_external_items_pre_schema_null_row_surfaces_to_each_tenant():
    """Pre-schema rows with NULL org_id are included under every org's
    view so the migration cutover doesn't blank the calendar."""
    item = {
        "platform": "YouTube Video", "external_id": "legacy",
        "iso_date": "2026-05-10", "scheduled_time": "10:00",
        "title": "legacy", "url": "", "status": "scheduled", "raw_json": "{}",
    }
    _db.upsert_external_items([item])  # org_id=None
    rows = _db.get_external_items_for_window("2026-05-01", "2026-05-31", org_id=99)
    assert any(r["external_id"] == "legacy" for r in rows)


# ── mark_stale scoped per-org ─────────────────────────────────────────


def test_mark_stale_does_not_cross_orgs():
    """A refresh for org A must not mark org B's items as deleted."""
    a_item = {
        "platform": "YouTube Video", "external_id": "a-1",
        "iso_date": "2026-05-10", "scheduled_time": "", "title": "", "url": "",
        "status": "scheduled", "raw_json": "{}",
    }
    b_item = {**a_item, "external_id": "b-1"}
    _db.upsert_external_items([a_item], org_id=1)
    _db.upsert_external_items([b_item], org_id=2)
    # Org 1 refreshes with NO seen ids — its row should flip to deleted,
    # org 2's must stay alive.
    _db.mark_stale_external_items(
        "YouTube Video", "2026-05-01", "2026-05-31",
        seen_ids=set(), org_id=1,
    )
    a_rows = _db.get_external_items_for_window(
        "2026-05-01", "2026-05-31", org_id=1,
    )
    b_rows = _db.get_external_items_for_window(
        "2026-05-01", "2026-05-31", org_id=2,
    )
    # Org 1: deleted row not returned (status='deleted' filtered out).
    assert all(r["external_id"] != "a-1" for r in a_rows)
    # Org 2: row still present.
    assert any(r["external_id"] == "b-1" for r in b_rows)
