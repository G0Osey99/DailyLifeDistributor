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
import logging
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from core.calendar_refresh import ExternalItem
from core.quota import track_quota_usage

log = logging.getLogger(__name__)

# Browser-ish UA + a consent cookie. YouTube has no Data API field that says
# "this is a Short", and fileDetails.videoStreams (aspect ratio) comes back
# empty for this channel — so we detect Shorts by probing the /shorts/<id>
# URL: a real Short serves 200 there, a normal video 303-redirects to /watch.
# The cookie is required because EU-egress IPs (the VPS is in the EU) otherwise
# bounce to consent.youtube.com, which masks the real redirect.
_PROBE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
_PROBE_COOKIE = "CONSENT=YES+1; SOCS=CAISEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"
_PROBE_TIMEOUT = 8.0
_PROBE_WORKERS = 8


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401 - suppress auto-follow
        return None


def _is_short(video_id: str) -> bool | None:
    """Return True/False if ``video_id`` is/ isn't a YouTube Short, or None if
    we can't tell (network error, or a consent wall we couldn't bypass).

    Deterministic: GET(HEAD) https://www.youtube.com/shorts/<id> with redirects
    disabled. 200 → it's a Short; 30x to /watch → it's a normal video.
    """
    url = f"https://www.youtube.com/shorts/{video_id}"
    req = urllib.request.Request(url, method="HEAD", headers={
        "User-Agent": _PROBE_UA,
        "Cookie": _PROBE_COOKIE,
        "Accept-Language": "en-US,en;q=0.9",
    })
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        try:
            resp = opener.open(req, timeout=_PROBE_TIMEOUT)
            return resp.status == 200  # stayed on /shorts/ → Short
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location") or ""
                if "/watch" in loc:
                    return False
                if "consent." in loc:
                    return None  # consent wall — can't classify
                return None
            return None
    except Exception:  # noqa: BLE001 - any network failure → unknown
        return None


def _probe_shorts(video_ids: list[str]) -> dict[str, bool | None]:
    """Probe many ids in parallel; map id -> is_short (or None if unknown)."""
    if not video_ids:
        return {}
    with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as ex:
        results = list(ex.map(_is_short, video_ids))
    return dict(zip(video_ids, results))

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


def _search_owned_video_ids(yt, max_items: int = 100):
    """Discover owned video IDs (incl. private) via ``search.list(forMine=True)``.

    The uploads playlist omits *private* videos for the owner, so scheduled
    videos — which YouTube keeps ``private`` until their ``publishAt`` — never
    surface through :func:`_walk_uploads`. ``search.list`` with ``forMine=True``
    is the only API path that returns the owner's videos regardless of privacy,
    so it's how we pick up scheduled drafts. It costs 100 units/call, so we cap
    the walk; ``order=date`` surfaces freshly-created scheduled items first.
    """
    ids: list[str] = []
    page_token = None
    while len(ids) < max_items:
        resp = yt.search().list(
            part="id", forMine=True, type="video",
            order="date", maxResults=50, pageToken=page_token,
        ).execute()
        track_quota_usage("refresh_search_list")
        for it in resp.get("items", []):
            vid = (it.get("id") or {}).get("videoId")
            if vid:
                ids.append(vid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _classify(duration_seconds: int, is_short: bool | None = None) -> str:
    """Pick the platform for a video.

    Prefer the authoritative /shorts/ probe result (``is_short``). Only when
    that's unknown (None) do we fall back to the legacy duration heuristic —
    note that's unreliable on its own because Shorts can now run up to 3
    minutes, so a probe failure may misfile a long Short as a video.
    """
    if is_short is True:
        return "youtube_shorts"
    if is_short is False:
        return "youtube_video"
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
    walk_ids = list(_walk_uploads(yt, playlist_id, oldest_allowed=window_start, max_items=max_items))

    # The uploads playlist can't see private/scheduled videos, so do a second
    # forMine search pass to pick those up. Keep it best-effort: a search
    # failure must not lose the published items we already have.
    try:
        search_ids = _search_owned_video_ids(yt, max_items=max_items)
    except Exception as e:  # noqa: BLE001
        log.warning("youtube refresh: forMine search failed (%s); "
                    "scheduled videos may be missing this run", e, exc_info=True)
        search_ids = []

    seen: set[str] = set()
    video_ids: list[str] = []
    for vid in walk_ids + search_ids:
        if vid not in seen:
            seen.add(vid)
            video_ids.append(vid)

    # Authoritatively classify Shorts vs videos by probing /shorts/<id>
    # (duration alone can't — both run 1.5-3 min on this channel).
    short_map = _probe_shorts(video_ids)

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
            platform = _classify(duration_s, short_map.get(v["id"]))
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
