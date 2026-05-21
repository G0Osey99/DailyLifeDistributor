"""Parse stable platform ids out of canonical URLs.

All helpers return None on inputs they don't recognize. Never raise.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
_SIMPLECAST_UUID = re.compile(
    r"/episodes/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I
)
_ROCK_ITEM = re.compile(r"/ContentChannelItem/(\d+)", re.I)


def _youtube_id(url: str) -> str | None:
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.hostname not in _YT_HOSTS:
        return None
    if u.hostname in ("youtu.be", "www.youtu.be"):
        return (u.path.lstrip("/") or None)
    if u.path.startswith("/shorts/"):
        return u.path.split("/", 3)[2] or None
    if u.path == "/watch":
        v = parse_qs(u.query).get("v")
        return v[0] if v else None
    return None


def _simplecast_id(url: str) -> str | None:
    m = _SIMPLECAST_UUID.search(url or "")
    return m.group(1).lower() if m else None


def _rock_id(url: str) -> str | None:
    m = _ROCK_ITEM.search(url or "")
    return m.group(1) if m else None


_DISPATCH = {
    "youtube_video": _youtube_id,
    "youtube_shorts": _youtube_id,
    "youtube video": _youtube_id,
    "youtube shorts": _youtube_id,
    "youtube": _youtube_id,
    "simplecast": _simplecast_id,
    "podcast": _simplecast_id,
    "rock": _rock_id,
}


def parse_url(platform: str, url: str) -> str | None:
    """Return the stable external id for `(platform, url)`, or None.

    Platform matching is case-insensitive and tolerates both the slug form
    (``youtube_video``) and the display form (``YouTube Video``) since
    ``upload_history`` and ``external_calendar_items`` use different
    conventions.
    """
    if not url:
        return None
    key = (platform or "").strip().lower()
    fn = _DISPATCH.get(key)
    if fn is None:
        # Fallback: derive id from the URL itself when the platform label is
        # unfamiliar but the URL is recognizable.
        for guess in (_youtube_id, _simplecast_id, _rock_id):
            ext = guess(url)
            if ext:
                return ext
        return None
    return fn(url)
