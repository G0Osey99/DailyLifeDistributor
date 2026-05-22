"""Unit tests for core.refresh.youtube_source.fetch().

These exercise the discovery + status-mapping logic against a fake YouTube
client, with no network or quota DB writes. The key behaviour under test:
scheduled videos (private + future ``publishAt``) are *only* reachable through
the ``search.list(forMine=True)`` pass, because the uploads playlist omits
private videos for the owner.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from core.refresh import youtube_source


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Resource:
    """Fake googleapiclient resource: every method returns a _Req."""

    def __init__(self, handler):
        self._handler = handler

    def list(self, **kwargs):
        return _Req(self._handler(**kwargs))


class _FakeYouTube:
    def __init__(self, *, uploads_items, search_items, videos):
        self._uploads_items = uploads_items
        self._search_items = search_items
        self._videos = videos  # id -> video resource dict

    def channels(self):
        return _Resource(lambda **kw: {
            "items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UPL"}}}]
        })

    def playlistItems(self):
        return _Resource(lambda **kw: {"items": self._uploads_items})

    def search(self):
        return _Resource(lambda **kw: {"items": self._search_items})

    def videos(self):
        def handler(**kw):
            ids = kw["id"].split(",")
            return {"items": [self._videos[i] for i in ids if i in self._videos]}
        return _Resource(handler)


@pytest.fixture(autouse=True)
def _no_quota_writes(monkeypatch):
    monkeypatch.setattr(youtube_source, "track_quota_usage", lambda *a, **k: None)


def _video(vid, *, privacy, published_at=None, publish_at=None, duration="PT2M0S", title="t"):
    status = {"privacyStatus": privacy}
    if publish_at:
        status["publishAt"] = publish_at
    snippet = {"title": title}
    if published_at:
        snippet["publishedAt"] = published_at
    return {
        "id": vid,
        "status": status,
        "snippet": snippet,
        "contentDetails": {"duration": duration},
    }


def test_fetch_emits_published_and_scheduled(monkeypatch):
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=5)
    future = now + timedelta(days=10)

    videos = {
        "pub1": _video("pub1", privacy="public", published_at=_iso(past), duration="PT2M0S"),
        "sched1": _video("sched1", privacy="private", publish_at=_iso(future), duration="PT0M30S"),
    }
    fake = _FakeYouTube(
        # Uploads playlist only exposes the public/past video (private omitted).
        uploads_items=[{"contentDetails": {"videoId": "pub1", "videoPublishedAt": _iso(past)},
                        "snippet": {"publishedAt": _iso(past)}}],
        # forMine search surfaces the scheduled private video — and re-lists pub1
        # (dedup must collapse it).
        search_items=[{"id": {"videoId": "sched1"}}, {"id": {"videoId": "pub1"}}],
        videos=videos,
    )
    monkeypatch.setattr(youtube_source, "_build_client", lambda: fake)

    today = date.today()
    items = youtube_source.fetch(today - timedelta(days=30), today + timedelta(days=180))

    by_id = {it.external_id: it for it in items}
    assert set(by_id) == {"pub1", "sched1"}  # dedup collapsed the duplicate pub1
    assert by_id["pub1"].status == "published"
    assert by_id["pub1"].platform == "youtube_video"
    assert by_id["sched1"].status == "scheduled"
    assert by_id["sched1"].platform == "youtube_shorts"
    assert by_id["sched1"].iso_date == future.date().isoformat()


def test_fetch_filters_outside_window(monkeypatch):
    now = datetime.now(timezone.utc)
    far_future = now + timedelta(days=400)  # beyond the 180-day forward window

    videos = {
        "far1": _video("far1", privacy="private", publish_at=_iso(far_future)),
    }
    fake = _FakeYouTube(
        uploads_items=[],
        search_items=[{"id": {"videoId": "far1"}}],
        videos=videos,
    )
    monkeypatch.setattr(youtube_source, "_build_client", lambda: fake)

    today = date.today()
    items = youtube_source.fetch(today - timedelta(days=30), today + timedelta(days=180))
    assert items == []


def test_fetch_survives_search_failure(monkeypatch):
    """A forMine search error must not drop the published items already walked."""
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=3)
    videos = {"pub1": _video("pub1", privacy="public", published_at=_iso(past))}

    class _BrokenSearch(_FakeYouTube):
        def search(self):
            def boom(**kw):
                raise RuntimeError("quota exceeded")
            return _Resource(boom)

    fake = _BrokenSearch(
        uploads_items=[{"contentDetails": {"videoId": "pub1", "videoPublishedAt": _iso(past)},
                        "snippet": {"publishedAt": _iso(past)}}],
        search_items=[],
        videos=videos,
    )
    monkeypatch.setattr(youtube_source, "_build_client", lambda: fake)

    today = date.today()
    items = youtube_source.fetch(today - timedelta(days=30), today + timedelta(days=180))
    assert [it.external_id for it in items] == ["pub1"]
    assert items[0].status == "published"
