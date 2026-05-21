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


def merge_for_window(history_rows: list[dict], external_rows: list[dict]) -> list[dict]:
    """Return one merged list with each item appearing exactly once.

    Priority rules:

    1. **External rows win on (provider, iso_date) for failures.** An
       external row reflects what's actually scheduled on the platform —
       the source of truth. If the user fixed a failed upload by
       scheduling the episode/video manually on the platform, the stale
       failure in upload_history is no longer meaningful, so we drop it.
       This applies whether or not the external_ids match (a manually-
       created item won't have the same id as our failed attempt).

    2. **Successful history rows dedupe externals by (provider,
       external_id).** When both tables describe the same successful
       upload, prefer the upload row (it has richer local context like
       ``file_path``).

    Each returned row carries ``source`` ∈ {'upload', 'external'}.
    """
    # Build a (provider, iso_date) set of external coverage so we can
    # suppress stale failures.
    external_buckets: set[tuple[str, str]] = set()
    for e in external_rows:
        provider = _provider(e.get("platform") or "")
        iso_date = e.get("iso_date") or ""
        if provider and iso_date:
            external_buckets.add((provider, iso_date))

    seen_external_id_keys: set[tuple[str, str]] = set()
    out: list[dict] = []
    for h in history_rows:
        provider = _provider(h.get("platform") or "")
        iso_date = h.get("iso_date") or ""
        is_failure = _is_failure(h)

        # Stale-failure suppression: if the platform now has an item for
        # this (provider, date), the failed local row is obsolete.
        if is_failure and provider and iso_date and (provider, iso_date) in external_buckets:
            continue

        ext_id = h.get("external_id") or ""
        if ext_id:
            seen_external_id_keys.add((provider, ext_id))
        h2 = dict(h)
        h2["source"] = "upload"
        out.append(h2)

    for e in external_rows:
        ext_id = e.get("external_id") or ""
        if ext_id and (_provider(e.get("platform") or ""), ext_id) in seen_external_id_keys:
            continue
        e2 = dict(e)
        e2["source"] = "external"
        out.append(e2)
    return out
