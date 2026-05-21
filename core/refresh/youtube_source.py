"""Fetch recent and scheduled videos from the authenticated channel.

We surface every upload in the refresh window — published, scheduled,
unlisted, and private (drafts) alike — so the calendar is a full mirror
of the channel's state, not just "what's going public next."

Status mapping (kept narrow so the UI can colour-code reliably):
  - public + publishedAt in past   → 'published'
  - private + status.publishAt set → 'scheduled' (uses publishAt)
  - public + publishedAt in future → 'scheduled' (premieres / future-public)
  - everything else                → 'draft' (private/unlisted/no schedule)

Each YouTube API call charges quota via ``core.quota.track_quota_usage``
so the persistent daily counter reflects refresh activity, not just
uploads. Per the YouTube Data API quota table: channels.list /
playlistItems.list / videos.list each cost 1 unit per call.

Shorts vs Video: ``contentDetails.duration`` ≤60s → 'youtube_shorts',
else 'youtube_video'.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

from core.calendar_refresh import ExternalItem
from core.quota import track_quota_usage

NAME = "youtube"
PLATFORMS = ["youtube_video", "youtube_shorts"]

_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _iso_duration_to_seconds(iso: str) -> int:
    m = _DURATION_RE.fullmatch(iso or "")
    if not m:
        return 0
    h, mn, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + s


def _build_client():
    """Reuse the existing uploader's authed client builder."""
    from uploaders import youtube_uploader
    return youtube_uploader.get_authenticated_service()


def _uploads_playlist_id(yt) -> str:
    resp = yt.channels().list(part="contentDetails", mine=True).execute()
    track_quota_usage("refresh_channels_list")
    return resp["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]


def _walk_uploads(yt, playlist_id: str, oldest_allowed: date, max_items: int = 200):
    """Yield videoIds from the uploads playlist, stopping when items get too old.

    The uploads playlist is ordered newest-first by ``publishedAt``. For
    drafts/unlisted videos without a publishedAt yet, we don't have a date
    to compare against — yield them and let the caller window-filter on
    the per-video metadata.
    """
    page_token = None
    yielded = 0
    while True:
        resp = yt.playlistItems().list(
            playlistId=playlist_id, part="contentDetails,snippet",
            maxResults=50, pageToken=page_token,
        ).execute()
        track_quota_usage("refresh_playlist_items_list")
        for it in resp.get("items", []):
            published_at = it["contentDetails"].get("videoPublishedAt") \
                or it["snippet"].get("publishedAt")
            if published_at:
                try:
                    d = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
                    if d < oldest_allowed:
                        return
                except ValueError:
                    pass
            yield it["contentDetails"]["videoId"]
            yielded += 1
            if yielded >= max_items:
                return
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def _classify(duration_seconds: int) -> str:
    return "youtube_shorts" if 0 < duration_seconds <= 60 else "youtube_video"


def _resolve_date_and_status(status_obj: dict, snippet: dict) -> tuple[str, str, str] | None:
    """Pick the best (iso_date, scheduled_iso, status) for a video.

    Returns None if we genuinely can't place it on a calendar day.
    """
    privacy = status_obj.get("privacyStatus")
    publish_at = status_obj.get("publishAt")          # future-scheduled (private→public)
    published_at = snippet.get("publishedAt")          # actual publish time
    now = datetime.now(timezone.utc)

    if privacy == "public" and published_at:
        pub_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        status = "scheduled" if pub_dt > now else "published"
        return pub_dt.date().isoformat(), pub_dt.isoformat(), status

    if privacy == "private" and publish_at:
        pub_dt = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
        return pub_dt.date().isoformat(), pub_dt.isoformat(), "scheduled"

    # Drafts/unlisted/private-without-schedule: anchor to publishedAt if
    # YouTube has stamped one (it does for unlisted), else skip — there's
    # no day to put it on.
    if published_at:
        pub_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        return pub_dt.date().isoformat(), pub_dt.isoformat(), "draft"

    return None


def fetch(window_start: date, window_end: date, max_items: int = 200) -> list[ExternalItem]:
    yt = _build_client()
    playlist_id = _uploads_playlist_id(yt)
    video_ids = list(_walk_uploads(yt, playlist_id, oldest_allowed=window_start, max_items=max_items))
    out: list[ExternalItem] = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = yt.videos().list(
            part="status,contentDetails,snippet",
            id=",".join(batch),
        ).execute()
        track_quota_usage("refresh_videos_list")
        for v in resp.get("items", []):
            status_obj = v.get("status", {})
            snippet = v.get("snippet", {})
            content = v.get("contentDetails", {})

            resolved = _resolve_date_and_status(status_obj, snippet)
            if resolved is None:
                continue
            iso_d, sched_iso, status = resolved

            d = datetime.fromisoformat(iso_d).date()
            if not (window_start <= d <= window_end):
                continue

            duration_s = _iso_duration_to_seconds(content.get("duration", ""))
            platform = _classify(duration_s)
            out.append(ExternalItem(
                platform=platform,
                external_id=v["id"],
                iso_date=iso_d,
                scheduled_time=sched_iso,
                title=snippet.get("title", ""),
                url=f"https://www.youtube.com/watch?v={v['id']}",
                status=status,
                raw_json=json.dumps({
                    "duration_s": duration_s,
                    "privacy": status_obj.get("privacyStatus"),
                }),
            ))
    return out
