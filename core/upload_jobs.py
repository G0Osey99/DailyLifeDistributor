"""Upload job registry + per-job runner.

The upload page kicks off a background thread that runs all per-platform
uploads in parallel and pushes SSE events into a per-job queue. This module
owns the job dict, the runner, and the stale-job reaper. Routes import
from here; nothing here imports from Flask blueprints.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import db as _db
from core.config import load_config
from core.playwright_session import SessionExpiredError
from core.session_state import session
from uploaders.youtube_uploader import upload_video as yt_upload_video
from uploaders.simplecast_uploader import upload_episode as sc_upload_episode
from uploaders.rock import upload_daily_experience as rock_upload_de
from uploaders.rock import schedule_email as rock_schedule_email
from uploaders.vista_social_uploader import upload_post as vs_upload_post


# job_id -> {"queue": Queue, "done": bool, "finished_at": float|None}
#
# IMPORTANT: this dict is in-process state. Under a multi-worker WSGI server
# (gunicorn -w N, uwsgi --processes N), each worker has its own _jobs and the
# SSE stream will hit "Job not found" on whichever worker happens to handle
# the GET /upload/stream request. The launch script runs `python app.py`
# (single-process Flask dev server), so this is fine in production; the
# warning below catches anyone who tries to deploy behind a real WSGI server.
_jobs: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
# After a job emits "done", keep its queue alive for this many seconds so the
# SSE consumer (which may reconnect) can still drain remaining events.
_JOB_RETENTION_SECONDS = 300
# How often the background reaper sweeps stale jobs. The previous design only
# reaped on new job creation, which left finished jobs sitting in memory if
# the user never started another upload.
_REAPER_INTERVAL_SECONDS = 60
# Bound the queue so a closed-tab SSE consumer can't pin GBs of progress
# events in memory. 1000 entries is ~5 GB of upload at 5 MB chunk granularity
# — well past anything realistic. Lossy events (progress) drop on full;
# milestone events (start/success/error/done) always block-put.
_QUEUE_MAXSIZE = 1000
_LOSSY_EVENT_TYPES = {"upload_progress", "progress", "phase_change"}

_log = logging.getLogger(__name__)

# One-time guard: warn (loudly) if we appear to be running under a real
# multi-worker WSGI server. _jobs cannot work across workers, so the user
# would otherwise just see mysterious "Job not found" errors mid-upload.
_multiworker_checked = False


def _warn_if_multiworker() -> None:
    global _multiworker_checked
    if _multiworker_checked:
        return
    _multiworker_checked = True
    server = (os.environ.get("SERVER_SOFTWARE", "") or "").lower()
    suspect = any(s in server for s in ("gunicorn", "uwsgi", "uvicorn", "mod_wsgi"))
    if suspect:
        _log.warning(
            "DailyLifeDistributor: detected WSGI server %r — upload jobs "
            "are stored per-process. Run with a single worker (e.g. "
            "`gunicorn -w 1`) or SSE clients will hit 'Job not found' on "
            "whichever worker handles their stream connection.",
            server,
        )


# Background reaper thread. Started lazily on the first job registration so
# tests that import this module don't spawn a thread for free.
_reaper_thread: threading.Thread | None = None
_reaper_started_lock = threading.Lock()


def _reaper_loop() -> None:
    while True:
        try:
            time.sleep(_REAPER_INTERVAL_SECONDS)
            reap_stale_jobs()
        except Exception:
            # A reaper crash should never take down the process. Log and keep
            # spinning — the next sweep will catch up.
            _log.exception("upload_jobs reaper sweep failed")


def _ensure_reaper_running() -> None:
    global _reaper_thread
    if _reaper_thread is not None and _reaper_thread.is_alive():
        return
    with _reaper_started_lock:
        if _reaper_thread is not None and _reaper_thread.is_alive():
            return
        t = threading.Thread(
            target=_reaper_loop, name="upload-jobs-reaper", daemon=True
        )
        t.start()
        _reaper_thread = t


def get_job(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        return _jobs.get(job_id)


def register_job(job_id: str) -> dict:
    _warn_if_multiworker()
    _ensure_reaper_running()
    job = {"queue": queue.Queue(maxsize=_QUEUE_MAXSIZE), "done": False, "finished_at": None}
    with _JOBS_LOCK:
        _jobs[job_id] = job
    return job


def drop_job(job_id: str) -> None:
    with _JOBS_LOCK:
        _jobs.pop(job_id, None)


def reap_stale_jobs() -> None:
    """Drop _jobs entries that finished more than _JOB_RETENTION_SECONDS ago.

    Called opportunistically on every new job creation. Cheap because the
    dict is small (one entry per active upload job), and it bounds memory in
    the case where a user closes the SSE tab before draining.
    """
    cutoff = time.time() - _JOB_RETENTION_SECONDS
    with _JOBS_LOCK:
        stale = [jid for jid, j in _jobs.items()
                 if j.get("done") and (j.get("finished_at") or 0) < cutoff]
        for jid in stale:
            _jobs.pop(jid, None)


# How long the Rock Email row will wait for its YouTube Video sibling to
# finish before giving up. Generous: a large video upload + processing.
_YT_WAIT_TIMEOUT_S = 30 * 60
_YT_POLL_INTERVAL_S = 1.0


def _resolve_youtube_watch_url(iso_date, emit_phase):
    """Block until this date's YouTube Video upload result is available, then
    return (watch_url, error). Only called when a YT Video upload is expected
    for the date. Returns ("", reason) if it failed/timed out. Reads the
    shared session.upload_results, written by record_result as rows finish.
    """
    deadline = time.time() + _YT_WAIT_TIMEOUT_S
    emit_phase("waiting_for_youtube")
    while time.time() < deadline:
        res = session.upload_results.get(iso_date, {}).get("YouTube Video")
        if res is not None:
            if res.get("skipped"):
                return "", "YouTube Video was skipped for this date."
            if res.get("success") and res.get("url"):
                return res["url"], ""
            return "", (
                "YouTube Video upload did not succeed for this date "
                f"({res.get('error') or 'unknown error'}); cannot schedule email."
            )
        time.sleep(_YT_POLL_INTERVAL_S)
    return "", "Timed out waiting for the YouTube Video upload to finish."


def _dispatch_upload(platform, entry, elements, emit, effective_row, item,
                     iso_date, yt_video_expected):
    """Run the uploader for one (date, platform) item and return its result.

    Shared by the legacy whole-session runner and the per-batch runner so the
    platform dispatch + progress events live in one place. Emits the same
    progress / phase_change events both paths always have.
    """
    def _yt_progress_cb(percent, bytes_sent, bytes_total, eta_seconds):
        emit({
            "type": "upload_progress",
            "row": effective_row,
            "date": item["date"],
            "platform": item["platform"],
            "percent": percent,
            "bytes_sent": bytes_sent,
            "bytes_total": bytes_total,
            "eta_seconds": eta_seconds,
        })

    def _yt_event_cb(payload):
        payload.setdefault("row", effective_row)
        payload.setdefault("date", item["date"])
        payload.setdefault("platform", item["platform"])
        emit(payload)

    def _phase(phase):
        emit({
            "type": "phase_change",
            "row": effective_row,
            "date": item["date"],
            "platform": item["platform"],
            "phase": phase,
        })

    if platform == "YouTube Video":
        emit({"type": "progress", "row": effective_row, "percent": 0, "message": "Starting YouTube video upload..."})
        return yt_upload_video(entry, is_short=False, elements=elements,
                               progress_callback=_yt_progress_cb, event_callback=_yt_event_cb)
    if platform == "YouTube Shorts":
        emit({"type": "progress", "row": effective_row, "percent": 0, "message": "Starting YouTube Shorts upload..."})
        return yt_upload_video(entry, is_short=True, elements=elements,
                               progress_callback=_yt_progress_cb, event_callback=_yt_event_cb)
    if platform == "SimpleCast":
        emit({"type": "progress", "row": effective_row, "percent": 0, "message": "Starting SimpleCast upload..."})
        return sc_upload_episode(entry, elements=elements)
    if platform == "Rock":
        emit({"type": "progress", "row": effective_row, "percent": 0,
              "message": "Starting Rock Daily Experience build..."})
        return rock_upload_de(entry, elements=elements, progress_callback=_phase)
    if platform == "Rock Email":
        emit({"type": "progress", "row": effective_row, "percent": 0,
              "message": "Preparing Daily Life email..."})
        # Resolve the horizontal YouTube watch link. If a YouTube Video upload
        # is part of this run for the date, wait for it; otherwise
        # schedule_email falls back to entry.youtube_watch_url.
        watch_url = ""
        if yt_video_expected.get(iso_date):
            watch_url, yt_err = _resolve_youtube_watch_url(iso_date, _phase)
            if not watch_url:
                return {"success": False, "skipped": False, "url": "",
                        "scheduled_time": "", "error": yt_err}
        return rock_schedule_email(entry, youtube_watch_url=watch_url,
                                   elements=elements, progress_callback=_phase)
    if platform == "Vista Social":
        emit({"type": "progress", "row": effective_row, "percent": 0,
              "message": "Starting Vista Social schedule..."})
        return vs_upload_post(entry, elements=elements, progress_callback=_phase)
    # H2: unknown platform — surface explicitly instead of silently dropping.
    return {"success": False, "error": f"Unknown platform {platform!r}"}


def _build_yt_video_expected(summary, skip_set):
    """Which dates have a (non-skipped) YouTube Video upload in this run.

    The Rock Email row for such a date waits for that upload's watch link.
    Dates without a YouTube Video row fall back to entry.youtube_watch_url.
    """
    expected: dict[str, bool] = {}
    for it in summary:
        if it.get("platform") == "YouTube Video":
            rid = f"{it['date']}_YouTube Video"
            iso = it.get("iso_date", it["date"])
            expected[iso] = rid not in skip_set
    return expected


# Maps a media category to the ReviewEntry path field it populates.
_CATEGORY_FIELD = {
    "youtube_video": "youtube_video_path",
    "youtube_shorts": "youtube_shorts_path",
    "podcast": "podcast_path",
    "thumbnails": "thumbnail_path",
    "email_thumbnails": "email_thumbnail_path",
}


def run_batch(
    dates: list,
    summary: list,
    file_paths: dict,
    session_id: str,
    emit,
    entries_snapshot: dict,
    skip_set: set | None = None,
    logger=None,
    config: dict | None = None,
) -> set:
    """Run one batch of dates against reassembled temp file paths.

    The browser-streaming pipeline's upload runner:
      * Points each ReviewEntry's path fields at this batch's temp files
        (``file_paths`` keyed by ``(category, iso_date)``).
      * Idempotently skips any ``(date, platform)`` already recorded as a
        success in ``upload_history`` for this session — re-running a partly
        completed batch never double-uploads.
      * Dedupes by physical file so a file shared by two platforms is counted
        (and later deleted by the caller) exactly once.
      * Preserves the email-waits-for-YouTube ordering within the batch.

    ``emit`` is the SSE emit callback (reuse the job queue's). Returns the set
    of distinct physical temp file paths consumed, so the caller can delete
    each once after the batch finishes.
    """
    skip_set = set(skip_set or set())
    if logger is None:
        logger = logging.getLogger(__name__)
    config = config or load_config()
    max_workers = config.get("upload", {}).get("max_workers", 4)

    # Point entry path fields at this batch's temp files.
    for (category, iso_date), path in file_paths.items():
        entry = entries_snapshot.get(iso_date)
        field = _CATEGORY_FIELD.get(category)
        if entry is not None and field and path:
            setattr(entry, field, path)

    # Dedup by physical file: a file shared by two platforms is consumed —
    # and later deleted — exactly once.
    distinct_files = {p for p in file_paths.values() if p}

    batch_dates = set(dates)
    batch_summary = [it for it in summary
                     if it.get("iso_date", it["date"]) in batch_dates]

    # Idempotent skip: a (date, platform) already recorded success is skipped.
    for it in batch_summary:
        iso = it.get("iso_date", it["date"])
        if _db.has_successful_upload(session_id, iso, it["platform"]):
            skip_set.add(f"{it['date']}_{it['platform']}")

    yt_video_expected = _build_yt_video_expected(batch_summary, skip_set)

    def emit_safe(payload):
        try:
            emit(payload)
        except Exception:
            logger.exception("batch emit failed")

    def _upload_one(idx, item):
        platform = item["platform"]
        row_id = f"{item['date']}_{platform}"
        if row_id in skip_set:
            emit_safe({"type": "skip", "row": idx, "platform": platform, "date": item["date"]})
            return idx, item, None
        emit_safe({"type": "start", "row": idx, "platform": platform,
                   "date": item["date"], "title": item.get("title", "")})
        iso_date = item.get("iso_date", item["date"])
        entry = entries_snapshot.get(iso_date)
        if entry is None:
            emit_safe({"type": "error", "row": idx, "date": item["date"],
                       "platform": platform, "message": "Entry not found"})
            return idx, item, None
        result = _dispatch_upload(platform, entry, entry.elements, emit_safe,
                                  idx, item, iso_date, yt_video_expected)
        return idx, item, result

    future_to_item: dict = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for idx, item in enumerate(batch_summary):
            try:
                f = executor.submit(_upload_one, idx, item)
            except Exception as exc:
                emit_safe({"type": "error", "row": idx, "date": item["date"],
                           "platform": item["platform"],
                           "message": f"Failed to schedule upload: {exc}"})
                continue
            future_to_item[f] = (idx, item)

        for future in as_completed(future_to_item):
            idx, item = future_to_item[future]
            try:
                _, _, result = future.result()
            except SessionExpiredError:
                emit_safe({"type": "error", "row": idx, "date": item["date"],
                           "platform": item["platform"],
                           "message": (f"Session expired for {item['platform']}. Open Settings "
                                       "and click 'Connect' to re-authenticate, then retry.")})
                continue
            except Exception as exc:
                emit_safe({"type": "error", "row": idx, "date": item["date"],
                           "platform": item["platform"], "message": str(exc)})
                continue

            iso_date = item.get("iso_date", item["date"])
            if result is None:
                row_id = f"{item['date']}_{item['platform']}"
                if session_id and row_id in skip_set:
                    try:
                        _db.record_upload(
                            session_id=session_id, iso_date=iso_date,
                            platform=item["platform"], title=item.get("title", ""),
                            file_path=item.get("file", ""), success=False, url="",
                            scheduled_time=item.get("scheduled_time", ""),
                            error="skipped", external_id=None,
                        )
                    except Exception as e:
                        logger.warning("DB record_upload (skip) failed: %s", e)
                continue

            session.record_result(iso_date, item["platform"], result)
            if result.get("skipped"):
                emit_safe({"type": "skip", "row": idx, "date": item["date"], "platform": item["platform"]})
            elif result.get("success"):
                emit_safe({"type": "success", "row": idx, "date": item["date"],
                           "platform": item["platform"], "url": result.get("url", ""),
                           "scheduled_time": result.get("scheduled_time", "")})
            else:
                ev_type = "needs_manual" if result.get("needs_manual") else "error"
                emit_safe({"type": ev_type, "row": idx, "date": item["date"],
                           "platform": item["platform"], "url": result.get("url", ""),
                           "message": result.get("error") or "Unknown error"})

            if session_id:
                try:
                    _db.record_upload(
                        session_id=session_id, iso_date=iso_date,
                        platform=item["platform"], title=item.get("title", ""),
                        file_path=item.get("file", ""), success=bool(result.get("success")),
                        url=result.get("url", ""), scheduled_time=item.get("scheduled_time", ""),
                        error=result.get("error", ""), external_id=result.get("external_id"),
                    )
                except Exception as e:
                    logger.warning("DB record_upload failed: %s", e)
                    emit_safe({"type": "db_error", "row": idx, "date": item["date"],
                               "platform": item["platform"], "message": f"Persistence failed: {e}"})

    return distinct_files
