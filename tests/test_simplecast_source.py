"""Unit tests for core.refresh.simplecast_source row parsing.

Row shapes are taken verbatim from the live SimpleCast dashboard: published
episodes render with an *empty* badge, scheduled (future) episodes carry a
"Scheduled" badge. The date format is identical for both.
"""
from datetime import date

import pytest

from core.calendar_refresh import ExternalItem, SessionExpiredError
from core.refresh import simplecast_source as s


def test_fetch_guard_checks_store_not_file(monkeypatch):
    # The guard must consult the encrypted store (has_session), not the
    # transient on-disk file PlaywrightSession deletes after each run.
    monkeypatch.setattr(s, "has_session", lambda *a, **k: False)
    with pytest.raises(SessionExpiredError):
        s.fetch(date(2026, 1, 1), date(2026, 12, 31))


def test_classify_status():
    assert s._classify_status("Scheduled") == "scheduled"
    assert s._classify_status("scheduled") == "scheduled"
    assert s._classify_status("") == "published"      # published episodes have no badge
    assert s._classify_status("Published") == "published"
    assert s._classify_status("Draft") is None
    assert s._classify_status("Private") is None


# Real rows captured from the dashboard (today on the box was 2026-05-22).
_SCHEDULED_ROW = {
    "id": "11111111-1111-4111-8111-111111111111",
    "href": "https://dashboard.simplecast.com/x/episodes/11111111-1111-4111-8111-111111111111",
    "badge": "Scheduled",
    "rowText": "E650\n\t\n\t\nMost Bad Decisions Start With This\n\t\nMay 30, 2026 at 12:00 AM\nScheduled\n\t",
}
_PUBLISHED_ROW = {
    "id": "22222222-2222-4222-8222-222222222222",
    "href": "https://dashboard.simplecast.com/x/episodes/22222222-2222-4222-8222-222222222222",
    "badge": "",
    "rowText": "E653\n\t\n\t\nYou Might Have Told This Lie Today\n\t\nMay 21, 2026 at 12:00 AM\n\t",
}
_DRAFT_ROW = {
    "id": "33333333-3333-4333-8333-333333333333",
    "href": "https://dashboard.simplecast.com/x/episodes/33333333-3333-4333-8333-333333333333",
    "badge": "Draft",
    "rowText": "E999\n\t\n\t\nSome Draft Title\n\t\nMay 20, 2026 at 12:00 AM\nDraft\n\t",
}


def test_rows_to_items_emits_published_and_scheduled():
    items = s._rows_to_items(
        [_SCHEDULED_ROW, _PUBLISHED_ROW, _DRAFT_ROW],
        date(2026, 4, 22), date(2026, 11, 18),
    )
    by_id = {it.external_id: it for it in items}
    # Draft is dropped; published + scheduled both emitted.
    assert set(by_id) == {_SCHEDULED_ROW["id"], _PUBLISHED_ROW["id"]}

    sched = by_id[_SCHEDULED_ROW["id"]]
    assert isinstance(sched, ExternalItem)
    assert sched.status == "scheduled"
    assert sched.iso_date == "2026-05-30"
    assert sched.title == "Most Bad Decisions Start With This"
    assert sched.platform == "simplecast"

    pub = by_id[_PUBLISHED_ROW["id"]]
    assert pub.status == "published"
    assert pub.iso_date == "2026-05-21"
    assert pub.title == "You Might Have Told This Lie Today"


def test_rows_to_items_filters_window():
    # Window excludes both rows' dates.
    items = s._rows_to_items(
        [_SCHEDULED_ROW, _PUBLISHED_ROW],
        date(2026, 1, 1), date(2026, 1, 31),
    )
    assert items == []


def test_rows_to_items_skips_unparseable_date():
    bad = {"id": "44444444-4444-4444-8444-444444444444", "href": "u",
           "badge": "", "rowText": "E000\n\t\nNo date here\n\t"}
    assert s._rows_to_items([bad], date(2026, 1, 1), date(2026, 12, 31)) == []
