"""Tests for the Daily Life email orchestrator (uploaders.rock.email).

No Playwright: we stub RockBrowserClient with a fake that records the
EmailFields it's handed and returns a canned ItemRef. This covers the
business rules — link required, idempotency skip, thumbnail validation,
and correct field composition — without a browser.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import uploaders.rock.email as email_mod
from uploaders.rock import EmailFields, ItemRef, schedule_email, email_title
from core.session_state import ReviewEntry, UploadElements


class _FakeRock:
    """Context-manager stand-in for RockBrowserClient."""

    last_fields: EmailFields | None = None
    existing: ItemRef | None = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def find_existing_email_for_date(self, fields):
        type(self).last_fields = fields
        return type(self).existing

    def create_email_item(self, fields):
        type(self).last_fields = fields
        return ItemRef(id=4242)


@pytest.fixture
def fake_rock(monkeypatch):
    _FakeRock.last_fields = None
    _FakeRock.existing = None
    monkeypatch.setattr(email_mod, "RockBrowserClient", _FakeRock)
    return _FakeRock


def _entry(**kw):
    base = dict(date="2026-12-31", display_date="December 31, 2026",
                description="A devotional line.", youtube_watch_url="")
    base.update(kw)
    return ReviewEntry(**base)


def test_errors_when_no_youtube_link(fake_rock):
    res = schedule_email(_entry(), youtube_watch_url="")
    assert res["success"] is False
    assert "YouTube link" in res["error"]
    # Never opened the browser flow far enough to build fields.
    assert fake_rock.last_fields is None


def test_uses_run_watch_url_and_builds_fields(fake_rock):
    res = schedule_email(_entry(), youtube_watch_url="https://www.youtube.com/watch?v=ABC123")
    assert res["success"] is True
    assert res["skipped"] is False
    assert res["url"].endswith("/ContentChannelItem/4242")
    f = fake_rock.last_fields
    assert f.title == email_title(date(2026, 12, 31)) == "Daily Life December 31, 2026"
    assert f.start_date == date(2026, 12, 31)
    assert f.description == "A devotional line."
    assert f.youtube_watch_url == "https://www.youtube.com/watch?v=ABC123"
    assert f.thumbnail_path is None  # no email_thumbnail_path set on the entry


def test_falls_back_to_entry_provided_link(fake_rock):
    res = schedule_email(_entry(youtube_watch_url="https://youtu.be/zzz"), youtube_watch_url="")
    assert res["success"] is True
    assert fake_rock.last_fields.youtube_watch_url == "https://youtu.be/zzz"


def test_run_url_takes_priority_over_entry_link(fake_rock):
    res = schedule_email(
        _entry(youtube_watch_url="https://youtu.be/old"),
        youtube_watch_url="https://www.youtube.com/watch?v=new",
    )
    assert res["success"] is True
    assert fake_rock.last_fields.youtube_watch_url == "https://www.youtube.com/watch?v=new"


def test_skips_when_already_exists(fake_rock):
    fake_rock.existing = ItemRef(id=99)
    res = schedule_email(_entry(), youtube_watch_url="https://youtu.be/x")
    assert res["success"] is True
    assert res["skipped"] is True
    assert res["url"].endswith("/ContentChannelItem/99")


def test_missing_thumbnail_file_errors(fake_rock, tmp_path):
    e = _entry()
    e.email_thumbnail_path = str(tmp_path / "does_not_exist.jpg")
    res = schedule_email(e, youtube_watch_url="https://youtu.be/x")
    assert res["success"] is False
    assert "thumbnail not found" in res["error"].lower()


def test_thumbnail_disabled_skips_thumbnail(fake_rock, tmp_path):
    e = _entry()
    e.email_thumbnail_path = str(tmp_path / "missing.jpg")
    e.elements = UploadElements(rock_email_thumbnail=False)
    res = schedule_email(e, youtube_watch_url="https://youtu.be/x", elements=e.elements)
    # With the thumbnail element off we never look at the file -> success.
    assert res["success"] is True
    assert fake_rock.last_fields.thumbnail_path is None


def test_existing_thumbnail_file_is_passed(fake_rock, tmp_path):
    thumb = tmp_path / "email_thumb.jpg"
    thumb.write_bytes(b"\xff\xd8\xff")  # minimal JPEG-ish bytes
    e = _entry()
    e.email_thumbnail_path = str(thumb)
    res = schedule_email(e, youtube_watch_url="https://youtu.be/x")
    assert res["success"] is True
    assert fake_rock.last_fields.thumbnail_path == Path(str(thumb))


def test_get_summary_emits_rock_email_row():
    from core.session_state import SessionState
    s = SessionState()
    s.selected_dates = ["2026-12-31"]
    entry = _entry(youtube_watch_url="https://youtu.be/x")
    entry.platforms_enabled = {"rock_email": True}
    s.entries = {"2026-12-31": entry}
    rows = [r for r in s.get_summary() if r["platform"] == "Rock Email"]
    assert len(rows) == 1
    assert rows[0]["title"] == "Daily Life December 31, 2026"
    assert rows[0]["iso_date"] == "2026-12-31"
