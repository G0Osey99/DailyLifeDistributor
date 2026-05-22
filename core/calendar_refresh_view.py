"""Pure helper: merge upload_history rows + external_calendar_items rows for the calendar view.

Kept separate from app.py so it's unit-testable without Flask.
"""
from __future__ import annotations


# Maps every platform string we've ever stored (display form from
# upload_history, slug form from external_calendar_items) to a coarse
# provider bucket. The bucket is what dedup keys on so a YouTube Short
# logged as "YouTube Shorts" in upload_history matches a row classified as
# "youtube_video" in external_calendar_items when they share the same
# external_id (the YouTube videoId is unique across both buckets).
_PROVIDER = {
    "youtube_video": "youtube",
    "youtube_shorts": "youtube",
    "youtube video": "youtube",
    "youtube shorts": "youtube",
    "youtube": "youtube",
    "simplecast": "simplecast",
    "podcast": "simplecast",
    "rock": "rock",
}


def _provider(platform: str) -> str:
    return _PROVIDER.get((platform or "").strip().lower(), (platform or "").strip().lower())


def _is_failure(h: dict) -> bool:
    """True if a history dict represents a failed upload.

    Tolerates two row shapes:
      * raw upload_history rows with `success` (0/1) and `error`
      * pre-decorated calendar dicts with `_status` ('published' /
        'failed' / 'scheduled')
    """
    if h.get("_status") == "failed":
        return True
    if h.get("_status") in ("published", "scheduled"):
        return False
    # Raw row: failure = not success, or any non-empty error string.
    return (not bool(h.get("success"))) or bool(h.get("error"))


# Status priority for dedup: a published item always beats the same content
# still shown as scheduled, which beats a failed attempt. This is what turns a
# scheduled item into a published one day-over-day (one fewer scheduled, one
# more published) instead of showing the same content twice.
_STATUS_RANK = {"published": 3, "scheduled": 2, "failed": 1}


def _row_status(row: dict) -> str:
    """Coarse status for a row from either table: 'published'|'scheduled'|'failed'."""
    # `_status` is the calendar's pre-decorated form; `status` is the raw
    # external_calendar_items value. Accept either.
    s = (row.get("_status") or row.get("status") or "").strip().lower()
    if s in _STATUS_RANK:
        return s
    if _is_failure(row):
        return "failed"
    # A successful upload with no explicit status is effectively published.
    return "published"


def _status_rank(row: dict) -> int:
    return _STATUS_RANK.get(_row_status(row), 0)


def merge_for_window(history_rows: list[dict], external_rows: list[dict]) -> list[dict]:
    """Return one merged list with each piece of content appearing exactly once.

    Rules:

    1. **Stale-failure suppression.** A *failed* upload_history row is dropped
       when an external row covers the same ``(provider, iso_date)`` — the
       platform now has the item (the user fixed it manually), so the local
       failure is obsolete. Applies regardless of whether the ids match.

    2. **Dedup by ``(provider, external_id)`` with status priority.** The same
       content (same stable platform id — a YouTube videoId / SimpleCast
       episode id doesn't change when it goes live) is collapsed to one row,
       keeping the highest status: **published > scheduled > failed**. So a
       scheduled item that later publishes becomes a single *published* row
       rather than appearing as both. On a status tie the upload row wins (it
       carries richer local context like ``file_path``). Applies uniformly to
       every provider.

    Rows without an ``external_id`` can't be matched and are always kept.
    Each returned row carries ``source`` ∈ {'upload', 'external'}.
    """
    external_buckets: set[tuple[str, str]] = set()
    for e in external_rows:
        provider = _provider(e.get("platform") or "")
        iso_date = e.get("iso_date") or ""
        if provider and iso_date:
            external_buckets.add((provider, iso_date))

    # Phase 1 — stale-failure suppression. Survivors become dedup candidates.
    candidates: list[tuple[dict, str]] = []
    for h in history_rows:
        provider = _provider(h.get("platform") or "")
        iso_date = h.get("iso_date") or ""
        if _is_failure(h) and provider and iso_date and (provider, iso_date) in external_buckets:
            continue
        candidates.append((h, "upload"))
    for e in external_rows:
        candidates.append((e, "external"))

    # Phase 2 — dedup by (provider, external_id), highest status wins.
    best: dict[tuple[str, str], tuple] = {}
    order: list[tuple[str, str]] = []
    passthrough: list[tuple[dict, str]] = []
    for row, source in candidates:
        ext_id = row.get("external_id") or ""
        if not ext_id:
            passthrough.append((row, source))
            continue
        key = (_provider(row.get("platform") or ""), ext_id)
        # Compare on (status_rank, upload_preferred) so published beats
        # scheduled, and ties prefer the upload row.
        score = (_status_rank(row), 1 if source == "upload" else 0)
        cur = best.get(key)
        if cur is None:
            order.append(key)
            best[key] = (score, row, source)
        elif score > cur[0]:
            best[key] = (score, row, source)

    out: list[dict] = []
    for key in order:
        _score, row, source = best[key]
        r = dict(row)
        r["source"] = source
        out.append(r)
    for row, source in passthrough:
        r = dict(row)
        r["source"] = source
        out.append(r)
    return out
