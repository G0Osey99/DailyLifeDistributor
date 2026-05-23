"""Server-side dispatcher: builds a job_plan envelope for the agent path,
sends it through the relay, and ingests the result stream.

Mirrors core.upload_jobs.run_batch inputs but never calls uploaders
locally — execution happens on the paired agent. See
docs/superpowers/specs/2026-05-22-hybrid-upload-agent-phase3-design.md.
"""
from __future__ import annotations
import logging
from typing import Any
from core import db as _db
from core import secrets_store as _ss
from core import image_gatherer as _img

_PROTOCOL_VERSION = 1

# Which secrets_store keys each platform name requires.
# Keys absent from the store are silently omitted from the envelope.
# YouTube credentials are kv secrets (get_secret → str).
# Playwright sessions are blobs (get_blob → bytes) stored under
# "playwright.<basename_no_ext>" by core.playwright_session.
# Real key strings confirmed:
#   youtube.token / youtube.client_secrets  → uploaders/youtube_uploader.py:45-46
#   playwright.rock_session                 → core/playwright_session.py:65-68 + :101
#   playwright.simplecast_session           → core/playwright_session.py:65-68 + :99
#   playwright.vista_social_session         → core/playwright_session.py:65-68 + :100
_PLATFORM_KEYS: dict[str, tuple[str, ...]] = {
    "YouTube Video":  ("youtube.token", "youtube.client_secrets"),
    "YouTube Shorts": ("youtube.token", "youtube.client_secrets"),
    "Rock":           ("playwright.rock_session",),
    "Rock Email":     ("playwright.rock_session",),
    "Simplecast":     ("playwright.simplecast_session",),
    "Vista Social":   ("playwright.vista_social_session",),
}


def _fetch_credential(key: str) -> str | None:
    """Return the credential string for *key*, regardless of storage kind.

    kv secrets  → get_secret (str).
    blob secrets → get_blob (bytes, decoded UTF-8).
    Tries kv first; falls back to blob.  Returns None when neither is stored.
    """
    val = _ss.get_secret(key)
    if val is not None:
        return val
    raw = _ss.get_blob(key)
    if raw is not None:
        return raw.decode("utf-8")
    return None


def collect_credentials(*, platforms_in_use: set[str]) -> dict[str, str]:
    """Return only the secrets_store entries needed for the given platforms.
    Missing keys are silently omitted."""
    needed: set[str] = set()
    for p in platforms_in_use:
        needed.update(_PLATFORM_KEYS.get(p, ()))
    out: dict[str, str] = {}
    for key in sorted(needed):
        val = _fetch_credential(key)
        if val is not None:
            out[key] = val
    return out

# Path fields removed from a serialized ReviewEntry before send; the agent
# re-resolves them from its own scan map.
# Notes on the full list:
#   - youtube_video_path, youtube_shorts_path, podcast_path: primary media paths
#   - thumbnail_path: YouTube/SimpleCast thumbnail
#   - email_thumbnail_path: Rock email thumbnail (separate dir)
#   - spotlight_image_path, vista_image_path, reflection_image_path: planned
#     Rock image fields (not yet on ReviewEntry; harmless to include — stripped
#     only if present in the serialized dict)
_STRIPPED_PATH_FIELDS = frozenset((
    "youtube_video_path",
    "youtube_shorts_path",
    "podcast_path",
    "thumbnail_path",
    "email_thumbnail_path",
    "spotlight_image_path",
    "vista_image_path",
    "reflection_image_path",
))

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Active-job registry — maps job_id → {"queue": Queue}
# ---------------------------------------------------------------------------
import threading as _threading

_jobs: dict[str, dict] = {}
_jobs_lock = _threading.RLock()


def register_job(*, job_id: str, sse_queue, session_id: str | None = None) -> None:
    """Register an SSE queue for *job_id* so on_frame can route events to it.

    *session_id* is optional; when provided, ``success`` events will be
    written to ``upload_history`` via :func:`core.db.record_upload`.
    """
    with _jobs_lock:
        _jobs[job_id] = {"queue": sse_queue, "session_id": session_id}


def drop_job(job_id: str) -> None:
    """Remove a job from the registry (call when the SSE stream closes)."""
    with _jobs_lock:
        _jobs.pop(job_id, None)


def _job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def on_frame(frame: dict) -> None:
    """Route an incoming relay frame to the appropriate SSE queue.

    Currently handles type ``event``; other types (credentials_updated,
    image_used, pending_results_chunk) are logged as debug no-ops so that
    adding them in A7/A8/A9 is a small switch addition here.
    """
    ftype = frame.get("type")
    if ftype == "event":
        job = _job(frame.get("job_id", ""))
        if job is None:
            _logger.debug("agent_dispatch.on_frame: event for unknown job %s dropped",
                          frame.get("job_id"))
            return
        if frame.get("event") == "success" and job.get("session_id"):
            try:
                _db.record_upload(
                    job["session_id"],
                    frame.get("iso_date", ""),
                    frame.get("platform", ""),
                    frame.get("payload", {}).get("title", ""),
                    frame.get("payload", {}).get("file_path", ""),
                    True,
                    frame.get("payload", {}).get("watch_url") or frame.get("payload", {}).get("url", ""),
                    frame.get("payload", {}).get("scheduled_time"),
                    "",
                    frame.get("payload", {}).get("external_id"),
                )
            except Exception as exc:
                _logger.warning("record_upload failed: %s", exc)
        job["queue"].put({k: v for k, v in frame.items() if k not in ("v", "type", "job_id")})
        return
    elif ftype == "credentials_updated":
        key, value = frame.get("key"), frame.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            _logger.warning("credentials_updated: bad shape %r", frame)
            return
        try:
            if key.startswith("playwright."):
                _ss.set_blob(key, value.encode("utf-8"))
            else:
                _ss.set_secret(key, value)
        except Exception as e:
            _logger.warning("credentials_updated: write failed for %s: %s", key, e)
        return
    elif ftype == "image_used":
        try:
            _db.record_image_use(
                photo_id=frame["photo_id"],
                source=frame["source"],
                topic=frame["topic"],
                used_on_date=frame["used_on_date"],
                photographer=frame.get("photographer", ""),
                photo_url=frame.get("photo_url", ""),
            )
        except Exception as e:
            _logger.warning("record_image_use failed: %s", e)
        try:
            _img.append_credits_entry(
                used_on_date=frame["used_on_date"],
                source=frame["source"],
                photographer=frame.get("photographer", ""),
                photo_url=frame.get("photo_url", ""),
                topic=frame["topic"],
            )
        except Exception as e:
            _logger.warning("append_credits_entry failed: %s", e)
        return
    _logger.debug("agent_dispatch.on_frame: unhandled type %r", ftype)


def _strip_paths(entry_dict: dict) -> dict:
    return {k: v for k, v in entry_dict.items() if k not in _STRIPPED_PATH_FIELDS}


def build_envelope(
    *,
    job_id: str,
    rows: list[dict],
    entries: dict,        # iso_date -> ReviewEntry
    credentials: dict,    # secrets_store key -> blob string
    config: dict,
) -> dict:
    """Compose the job_plan envelope. Pure function; no I/O."""
    out_rows = []
    for r in rows:
        iso = r["iso_date"]
        entry = entries[iso]
        out_rows.append({
            "row_idx": r["row_idx"],
            "iso_date": iso,
            "platforms": list(r["platforms"]),
            "elements": r["elements"],
            "entry": _strip_paths(entry.to_dict()),
        })
    return {
        "v": 1,
        "type": "job_plan",
        "job_id": job_id,
        "protocol_version": _PROTOCOL_VERSION,
        "config": config,
        "rows": out_rows,
        "credentials": dict(credentials),
    }


def filter_done_rows(*, session_id: str, summary: list[dict]) -> list[dict]:
    """Drop platforms (and entire rows) already recorded as ``success``
    in upload_history.

    Input: session_id, summary = list of {"date": iso, "platforms": [...]}
    Output: list of {"row_idx": idx_in_summary, "iso_date": iso,
    "platforms": [remaining]}, entire row omitted if all platforms done.
    """
    out: list[dict] = []
    for idx, item in enumerate(summary):
        iso = item["date"]
        remaining = [
            p for p in item["platforms"]
            if not _db.has_successful_upload(session_id, iso, p)
        ]
        if remaining:
            out.append({"row_idx": idx, "iso_date": iso, "platforms": remaining})
    return out


# ---------------------------------------------------------------------------
# Device selection + relay dispatch
# ---------------------------------------------------------------------------
import uuid as _uuid
from core import devices as _devices
from core import relay as _relay


class NoAgentOnlineError(RuntimeError):
    """Raised when /upload?path=agent is invoked but no paired agent is online."""


def _pick_device() -> dict:
    """Return the most-recently-seen online device dict.

    Raises NoAgentOnlineError if no device qualifies (none paired, or all
    last-seen more than freshness_seconds ago).
    """
    dev = _devices.most_recently_seen_online()
    if dev is None:
        raise NoAgentOnlineError("no paired agent is online")
    return dev


def start(
    *,
    session_id: str,
    summary: list[dict],
    entries: dict,
    elements: dict,
    config: dict,
) -> str:
    """Filter done rows, bundle credentials, build the envelope, and send
    it through the relay to the chosen agent. Returns the new job_id."""
    job_id = _uuid.uuid4().hex
    rows = filter_done_rows(session_id=session_id, summary=summary)
    if not rows:
        _logger.info("agent_dispatch.start(job=%s): nothing to do", job_id)
        return job_id
    for r in rows:
        r["elements"] = elements
    platforms_in_use: set[str] = set()
    for r in rows:
        platforms_in_use.update(r["platforms"])
    creds = collect_credentials(platforms_in_use=platforms_in_use)
    envelope = build_envelope(
        job_id=job_id,
        rows=rows,
        entries=entries,
        credentials=creds,
        config=config,
    )
    device = _pick_device()
    _relay.send_to_device(device["name"], envelope)
    _logger.info("agent_dispatch.start(job=%s, device=%s, rows=%d)",
                 job_id, device["name"], len(rows))
    return job_id
