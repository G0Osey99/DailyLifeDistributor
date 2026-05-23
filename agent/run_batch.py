"""Agent-side orchestration: dispatches each (row, platform) to the bundled
uploaders. Copy-and-trim of core.upload_jobs.run_batch with the db.* calls
removed (server pre-applies idempotent skip; server records upload_history
from emitted success events).

B4: skeleton + parallel pool (real dispatch added in B5/B6).
B5: circuit breaker + email-after-YT ordering.
B6: real per-platform dispatch into bundled uploaders.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from core.circuit_breaker import get_breaker

try:
    from playwright.sync_api import TimeoutError as _PlaywrightTimeout  # type: ignore
except Exception:
    class _PlaywrightTimeout(Exception):  # type: ignore
        """Placeholder when Playwright isn't importable."""

# Infrastructure failures that count toward opening the breaker.
# Per-row data failures (missing file, bad title) should NOT trip it.
_INFRA_FAILURES = (
    _PlaywrightTimeout,
    ConnectionError,
    TimeoutError,
    OSError,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-run YouTube-done signalling (reset at the start of each run())
# ---------------------------------------------------------------------------
_yt_done: dict[int, threading.Event] = {}   # row_idx -> Event
_yt_url: dict[int, str | None] = {}          # row_idx -> watch_url or None
_yt_lock = threading.Lock()


def _reset_yt_state() -> None:
    global _yt_done, _yt_url
    with _yt_lock:
        _yt_done = {}
        _yt_url = {}


def _record_yt_result(row_idx: int, watch_url: str | None) -> None:
    with _yt_lock:
        _yt_url[row_idx] = watch_url
        ev = _yt_done.setdefault(row_idx, threading.Event())
        ev.set()


def _wait_yt(row_idx: int, timeout: float = 1800.0) -> str | None:
    with _yt_lock:
        ev = _yt_done.setdefault(row_idx, threading.Event())
    ev.wait(timeout=timeout)
    return _yt_url.get(row_idx)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _emit_capture(emit, captured: list):
    """Return a wrapper that appends to `captured` then forwards to `emit`."""
    def _wrap(frame):
        captured.append(frame)
        emit(frame)
    return _wrap


def _breaker_for(platform: str, cb_cfg: dict):
    return get_breaker(
        f"upload:{platform}",
        failure_threshold=int(cb_cfg.get("failure_threshold", 3)),
        recovery_timeout=float(cb_cfg.get("recovery_timeout_seconds", 120)),
    )


# ---------------------------------------------------------------------------
# _run_one — per (platform, row) worker with breaker + email-after-YT
# ---------------------------------------------------------------------------

def _run_one(platform: str, row: dict, emit, paths: dict, cb_cfg: dict) -> None:
    breaker = _breaker_for(platform, cb_cfg)
    if not breaker.allow():
        emit({"type": "event", "event": "error", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error": "circuit_breaker_open"})
        return

    # Rock Email must wait for this date's YouTube Video result (if present).
    if platform == "Rock Email" and "YouTube Video" in row.get("platforms", []):
        watch = _wait_yt(row["row_idx"])
        row = dict(row)   # shallow copy so we don't mutate the shared row
        row["yt_watch_url"] = watch

    captured: list = []
    try:
        _dispatch_upload(platform=platform, row=row,
                         emit=_emit_capture(emit, captured), paths=paths)
        # Determine success from emitted frames.
        if any(f.get("event") == "success" for f in captured):
            breaker.record_success()
            if platform == "YouTube Video":
                url = next(
                    (f.get("payload", {}).get("watch_url")
                     for f in captured if f.get("event") == "success"),
                    None,
                )
                _record_yt_result(row["row_idx"], url)
        else:
            # No success event — treat as data failure (neutral to breaker).
            if platform == "YouTube Video":
                _record_yt_result(row["row_idx"], None)

    except _INFRA_FAILURES as e:
        breaker.record_failure()
        emit({"type": "event", "event": "error", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error": str(e)})
        if platform == "YouTube Video":
            _record_yt_result(row["row_idx"], None)

    except Exception as e:
        # Data failure — neutral to breaker.
        emit({"type": "event", "event": "error", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error": str(e)})
        if platform == "YouTube Video":
            _record_yt_result(row["row_idx"], None)


# ---------------------------------------------------------------------------
# run() — public entry point
# ---------------------------------------------------------------------------

def run(*, envelope: dict, paths: dict, emit) -> None:
    """Execute the job plan. `paths` is {iso_date: {kind: local_path}}.
    `emit` is called once per event frame (dict)."""
    _reset_yt_state()

    rows = envelope["rows"]
    config = envelope.get("config", {})
    max_workers = int(config.get("max_workers", 4))
    cb_cfg: dict = config.get("circuit_breaker", {}) or {}

    # Propagate circuit-breaker config into each row (picked up by _run_one).
    if cb_cfg:
        rows = [dict(r, _config_circuit_breaker=cb_cfg) for r in rows]

    tasks = []
    for row in rows:
        for platform in row["platforms"]:
            tasks.append((platform, row))

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [
            ex.submit(_run_one, platform, row, emit, paths,
                      row.get("_config_circuit_breaker") or cb_cfg)
            for (platform, row) in tasks
        ]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                _logger.exception("run_batch task crashed: %s", e)

    emit({"type": "event", "event": "done", "job_id": envelope.get("job_id")})


# ---------------------------------------------------------------------------
# _dispatch_upload — real per-platform dispatch (B6)
# ---------------------------------------------------------------------------

def _make_elements(el_dict: dict):
    """Convert the serialized elements dict to an UploadElements object.
    Falls back gracefully: unknown keys are ignored, missing keys use defaults."""
    from core.session_state import UploadElements
    known = UploadElements.__dataclass_fields__.keys()
    kwargs = {k: bool(v) for k, v in el_dict.items() if k in known}
    return UploadElements(**kwargs)


def _entry_obj(row: dict):
    """Rebuild a lightweight ReviewEntry from the serialized dict.
    Path fields are injected from `paths` at the call site."""
    from core.session_state import ReviewEntry
    entry_data = dict(row["entry"])
    # Ensure display_date is present (required by ReviewEntry)
    if "display_date" not in entry_data:
        entry_data["display_date"] = entry_data.get("date", "")
    # Remove the nested elements dict if it slipped in (ReviewEntry can't
    # accept a plain dict for its UploadElements field).
    entry_data.pop("elements", None)
    # Drop any keys not in ReviewEntry's dataclass fields to avoid TypeError.
    known = set(ReviewEntry.__dataclass_fields__.keys())
    filtered = {k: v for k, v in entry_data.items() if k in known}
    return ReviewEntry(**filtered)


def _dispatch_upload(*, platform: str, row: dict, emit, paths: dict, **_) -> None:
    """Dispatch one (platform, row) to the appropriate bundled uploader.

    Emits start/success/error frames. Real uploader calls adapted from the
    reference implementation in core.upload_jobs._dispatch_upload.
    """
    from uploaders import youtube_uploader
    from uploaders import simplecast_uploader
    from uploaders.rock import orchestrator as rock_orch
    from uploaders.rock.email import schedule_email as rock_schedule_email
    from uploaders import vista_social_uploader

    iso = row["iso_date"]
    p = paths.get(iso, {})
    el_dict = row.get("elements") or {}
    elements = _make_elements(el_dict)

    emit({"type": "event", "event": "start", "platform": platform,
          "row_idx": row["row_idx"], "iso_date": iso})

    e = _entry_obj(row)

    if platform == "YouTube Video":
        e.youtube_video_path = p.get("video")
        e.thumbnail_path = p.get("thumbnail")

        def _progress_cb(percent, bytes_sent, bytes_total, eta_seconds):
            emit({"type": "event", "event": "upload_progress", "platform": platform,
                  "row_idx": row["row_idx"], "iso_date": iso,
                  "percent": percent, "bytes_sent": bytes_sent,
                  "bytes_total": bytes_total, "eta_seconds": eta_seconds})

        result = youtube_uploader.upload_video(
            e, is_short=False, elements=elements,
            progress_callback=_progress_cb,
        )

    elif platform == "YouTube Shorts":
        e.youtube_shorts_path = p.get("short_video")
        e.thumbnail_path = p.get("short_thumbnail")
        result = youtube_uploader.upload_video(e, is_short=True, elements=elements)

    elif platform in ("Simplecast", "SimpleCast"):
        e.podcast_path = p.get("audio")
        result = simplecast_uploader.upload_episode(e, elements=elements)

    elif platform == "Rock":
        e.youtube_video_path = p.get("video")
        e.thumbnail_path = p.get("thumbnail")
        result = rock_orch.upload_daily_experience(e, elements=elements)

    elif platform == "Rock Email":
        e.email_thumbnail_path = p.get("email_thumbnail")
        # Watch URL: prefer the resolved value from the YT wait, then the
        # entry field, then empty string.
        watch_url = (row.get("yt_watch_url")
                     or getattr(e, "youtube_watch_url", None)
                     or "")
        result = rock_schedule_email(e, youtube_watch_url=watch_url,
                                     elements=elements)

    elif platform == "Vista Social":
        e.youtube_video_path = p.get("video")
        result = vista_social_uploader.upload_post(e, elements=elements)

    else:
        result = {"success": False, "error": f"unknown platform {platform!r}"}

    if result is None:
        result = {"success": False, "error": "uploader returned None"}

    if result.get("success"):
        emit({"type": "event", "event": "success", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": iso,
              "payload": {k: v for k, v in result.items() if k != "success"}})
    else:
        emit({"type": "event", "event": "error", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": iso,
              "error": result.get("error", "unknown error")})
