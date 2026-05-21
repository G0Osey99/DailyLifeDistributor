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

from flask import current_app

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


def run_upload_job(
    job_id: str,
    skip_set: set,
    summary: list,
    entries_snapshot: dict,
    session_id: str = "",
    row_offset: int = 0,
    is_retry: bool = False,
    logger=None,
) -> None:
    """Background thread: runs all uploads in parallel and pushes SSE events into the job queue."""
    job = get_job(job_id)
    if job is None:
        return  # registration race; nothing to do
    q = job["queue"]
    config = load_config()
    max_workers = config.get("upload", {}).get("max_workers", 4)

    if logger is None:
        try:
            logger = current_app.logger
        except RuntimeError:
            import logging
            logger = logging.getLogger(__name__)

    def emit(payload: dict) -> None:
        msg = json.dumps(payload)
        if payload.get("type") in _LOSSY_EVENT_TYPES:
            # Drop progress chatter when the consumer has fallen behind so a
            # closed SSE tab can't pin chunks of upload progress in memory.
            try:
                q.put_nowait(msg)
            except queue.Full:
                return
        else:
            q.put(msg)

    # Which dates have a YouTube Video upload in *this* run (and not skipped)?
    # The Rock Email row for such a date waits for that upload's watch link
    # before it builds — enforcing "emails are scheduled after YouTube videos
    # within a flow". Dates without a YouTube Video row fall back to a
    # per-date provided link (entry.youtube_watch_url).
    yt_video_expected: dict[str, bool] = {}
    for _it in summary:
        if _it.get("platform") == "YouTube Video":
            _rid = f"{_it['date']}_YouTube Video"
            _iso = _it.get("iso_date", _it["date"])
            yt_video_expected[_iso] = _rid not in skip_set

    # How long the Rock Email row will wait for its YouTube Video sibling to
    # finish before giving up. Generous: a large video upload + processing.
    _YT_WAIT_TIMEOUT_S = 30 * 60
    _YT_POLL_INTERVAL_S = 1.0

    def _resolve_youtube_watch_url(iso_date, emit_phase):
        """Block until this date's YouTube Video upload result is available,
        then return (watch_url, error). Only called when a YT Video upload is
        expected for the date. Returns ("", reason) if it failed/timed out.
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

    def _upload_one(idx, item):
        """Upload a single platform item. Returns (idx, item, result)."""
        platform = item["platform"]
        row_id = f"{item['date']}_{platform}"
        effective_row = idx + row_offset
        if row_id in skip_set:
            emit({"type": "skip", "row": effective_row, "platform": platform, "date": item["date"]})
            return idx, item, None

        emit({"type": "start", "row": effective_row, "platform": platform, "date": item["date"], "title": item["title"]})

        iso_date = item.get("iso_date", item["date"])
        entry = entries_snapshot.get(iso_date)
        if entry is None:
            emit({"type": "error", "row": effective_row, "date": item["date"], "platform": item["platform"], "message": "Entry not found"})
            return idx, item, None

        elements = entry.elements

        def _yt_progress_cb(percent, bytes_sent, bytes_total, eta_seconds, _row=effective_row, _item=item):
            emit({
                "type": "upload_progress",
                "row": _row,
                "date": _item["date"],
                "platform": _item["platform"],
                "percent": percent,
                "bytes_sent": bytes_sent,
                "bytes_total": bytes_total,
                "eta_seconds": eta_seconds,
            })

        def _yt_event_cb(payload, _row=effective_row, _item=item):
            payload.setdefault("row", _row)
            payload.setdefault("date", _item["date"])
            payload.setdefault("platform", _item["platform"])
            emit(payload)

        result = None
        if platform == "YouTube Video":
            emit({"type": "progress", "row": effective_row, "percent": 0, "message": "Starting YouTube video upload..."})
            result = yt_upload_video(entry, is_short=False, elements=elements,
                                     progress_callback=_yt_progress_cb, event_callback=_yt_event_cb)
        elif platform == "YouTube Shorts":
            emit({"type": "progress", "row": effective_row, "percent": 0, "message": "Starting YouTube Shorts upload..."})
            result = yt_upload_video(entry, is_short=True, elements=elements,
                                     progress_callback=_yt_progress_cb, event_callback=_yt_event_cb)
        elif platform == "SimpleCast":
            emit({"type": "progress", "row": effective_row, "percent": 0, "message": "Starting SimpleCast upload..."})
            result = sc_upload_episode(entry, elements=elements)
        elif platform == "Rock":
            emit({"type": "progress", "row": effective_row, "percent": 0,
                  "message": "Starting Rock Daily Experience build..."})

            def _rock_progress(phase, _row=effective_row, _item=item):
                emit({
                    "type": "phase_change",
                    "row": _row,
                    "date": _item["date"],
                    "platform": _item["platform"],
                    "phase": phase,
                })

            result = rock_upload_de(entry, elements=elements, progress_callback=_rock_progress)
        elif platform == "Rock Email":
            emit({"type": "progress", "row": effective_row, "percent": 0,
                  "message": "Preparing Daily Life email..."})

            def _email_progress(phase, _row=effective_row, _item=item):
                emit({
                    "type": "phase_change",
                    "row": _row,
                    "date": _item["date"],
                    "platform": _item["platform"],
                    "phase": phase,
                })

            # Resolve the horizontal YouTube watch link. If a YouTube Video
            # upload is part of this run for the date, wait for it; otherwise
            # schedule_email falls back to entry.youtube_watch_url.
            watch_url = ""
            if yt_video_expected.get(iso_date):
                watch_url, yt_err = _resolve_youtube_watch_url(iso_date, _email_progress)
                if not watch_url:
                    result = {"success": False, "skipped": False, "url": "",
                              "scheduled_time": "", "error": yt_err}
            if result is None:
                result = rock_schedule_email(
                    entry,
                    youtube_watch_url=watch_url,
                    elements=elements,
                    progress_callback=_email_progress,
                )
        elif platform == "Vista Social":
            emit({"type": "progress", "row": effective_row, "percent": 0,
                  "message": "Starting Vista Social schedule..."})

            def _vs_progress(phase, _row=effective_row, _item=item):
                emit({
                    "type": "phase_change",
                    "row": _row,
                    "date": _item["date"],
                    "platform": _item["platform"],
                    "phase": phase,
                })

            result = vs_upload_post(entry, elements=elements, progress_callback=_vs_progress)
        else:
            # H2: unknown platform — surface explicitly instead of silently
            # dropping the row.
            result = {"success": False, "error": f"Unknown platform {platform!r}"}

        return idx, item, result

    # Wrap the entire executor flow in try/finally so that any
    # BaseException (KeyboardInterrupt, SystemExit) still marks the job
    # done and emits a terminal event — otherwise the SSE consumer hangs
    # on a queue.get(timeout=30) heartbeat loop forever, and the entry
    # never becomes eligible for reap_stale_jobs (which only collects
    # done=True). The previous version's plain `with ThreadPoolExecutor`
    # at the top level left this case unhandled.
    future_to_item: dict = {}
    try:
      with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for idx, item in enumerate(summary):
            try:
                f = executor.submit(_upload_one, idx, item)
            except Exception as exc:
                # H8: if submit itself fails, surface as a per-row error so
                # the UI doesn't silently lose the row.
                effective_row = idx + row_offset
                emit({"type": "error", "row": effective_row, "date": item["date"],
                      "platform": item["platform"], "message": f"Failed to schedule upload: {exc}"})
                continue
            future_to_item[f] = (idx, item)

        for future in as_completed(future_to_item):
            idx, item = future_to_item[future]
            effective_row = idx + row_offset
            try:
                _, _, result = future.result()
            except SessionExpiredError:
                emit({"type": "error", "row": effective_row, "date": item["date"],
                      "platform": item["platform"],
                      "message": (
                          f"Session expired for {item['platform']}. Open Settings "
                          "and click 'Connect' to re-authenticate, then retry."
                      )})
                continue
            except Exception as exc:
                emit({"type": "error", "row": effective_row, "date": item["date"],
                      "platform": item["platform"], "message": str(exc)})
                continue

            if result is None:
                # H1: worker returned None for skip-set or missing-entry path.
                # The skip/error event was already emitted inside _upload_one,
                # but skipped rows weren't getting written to upload_history,
                # so the calendar/history page never saw them. Persist a row.
                row_id = f"{item['date']}_{item['platform']}"
                if session_id and row_id in skip_set:
                    iso_date = item.get("iso_date", item["date"])
                    try:
                        _db.record_upload(
                            session_id=session_id,
                            iso_date=iso_date,
                            platform=item["platform"],
                            title=item.get("title", ""),
                            file_path=item.get("file", ""),
                            success=False,
                            url="",
                            scheduled_time=item.get("scheduled_time", ""),
                            error="skipped",
                            external_id=None,
                        )
                    except Exception as e:
                        logger.warning("DB record_upload (skip) failed: %s", e)
                        emit({"type": "db_error", "row": effective_row, "date": item["date"],
                              "platform": item["platform"], "message": f"Persistence failed: {e}"})
                continue

            iso_date = item.get("iso_date", item["date"])
            # Key by iso_date so `/results`, `/rescan`, and other lookups
            # (which all use iso) line up. The display-date key was a bug.
            # Funnel through record_result so the session's lock guards the
            # write — concurrent SSE/review/PATCH reads otherwise race here.
            session.record_result(iso_date, item["platform"], result)
            if result.get("skipped"):
                emit({"type": "skip", "row": effective_row, "date": item["date"], "platform": item["platform"]})
            elif result.get("success"):
                emit({
                    "type": "success",
                    "row": effective_row,
                    "date": item["date"],
                    "platform": item["platform"],
                    "url": result.get("url", ""),
                    "scheduled_time": result.get("scheduled_time", ""),
                })
            else:
                # H4: SimpleCast (and any future uploader) can land in a
                # "saved as draft, scheduling failed" state. Tag the SSE event
                # so the calendar/UI can show "needs manual action" instead of
                # a plain failure that hides the half-shipped artifact.
                ev_type = "needs_manual" if result.get("needs_manual") else "error"
                emit({
                    "type": ev_type,
                    "row": effective_row,
                    "date": item["date"],
                    "platform": item["platform"],
                    "url": result.get("url", ""),
                    "message": result.get("error") or "Unknown error",
                })

            if session_id:
                try:
                    # Prefer the uploader-supplied external_id when present
                    # (SimpleCast extracts the UUID from the post-save URL,
                    # for example) so we don't depend on URL parsing.
                    ext_id = result.get("external_id")
                    _db.record_upload(
                        session_id=session_id,
                        iso_date=iso_date,
                        platform=item["platform"],
                        title=item.get("title", ""),
                        file_path=item.get("file", ""),
                        success=bool(result.get("success")),
                        url=result.get("url", ""),
                        scheduled_time=item.get("scheduled_time", ""),
                        error=result.get("error", ""),
                        external_id=ext_id,
                    )
                except Exception as e:
                    # H6: surface DB persistence failures to the client so a
                    # successful upload that didn't get recorded isn't silent.
                    logger.warning("DB record_upload failed: %s", e)
                    emit({"type": "db_error", "row": effective_row, "date": item["date"],
                          "platform": item["platform"], "message": f"Persistence failed: {e}"})
    finally:
        # Mark session complete — but not for retry jobs, which only run a
        # single row and would otherwise close out a session whose other rows
        # are still running or pending.
        if session_id and not is_retry:
            try:
                _db.complete_session(session_id)
            except Exception as e:
                logger.warning("DB complete_session failed: %s", e)

        try:
            emit({"type": "done"})
        except Exception:
            # H5: log the underlying error rather than swallowing silently.
            logger.exception("emit(done) failed for job %s", job_id)
        with _JOBS_LOCK:
            existing = _jobs.get(job_id)
            if existing is not None:
                existing["done"] = True
                existing["finished_at"] = time.time()
