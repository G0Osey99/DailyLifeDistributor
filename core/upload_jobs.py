"""Upload job registry + per-job runner.

The upload page kicks off a background thread that runs all per-platform
uploads in parallel and pushes SSE events into a per-job queue. This module
owns the job dict, the runner, and the stale-job reaper. Routes import
from here; nothing here imports from Flask blueprints.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import db as _db
from core import platform_locks
from core.circuit_breaker import get_breaker
from core.config import load_config
from core.playwright_session import SessionExpiredError
from core.session_state import session

try:
    # Playwright's timeout (e.g. an unresponsive page during login/upload) is
    # an infra failure that should trip the circuit breaker. Imported
    # defensively so the module still loads where Playwright isn't installed.
    from playwright.sync_api import TimeoutError as _PlaywrightTimeout  # type: ignore
except Exception:  # pragma: no cover - playwright always present in prod
    class _PlaywrightTimeout(Exception):  # type: ignore
        """Placeholder when Playwright isn't importable."""

# Exceptions that indicate an *infrastructure* failure of the integration
# itself (broken/expired session, dead network, unresponsive page) as opposed
# to a per-row data problem (missing file, empty title). Only these trip the
# breaker, so a healthy platform is never disabled by a few bad rows.
_INFRA_FAILURES = (
    SessionExpiredError,
    _PlaywrightTimeout,
    ConnectionError,
    TimeoutError,
    OSError,
)
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
# — well past anything realistic. Emitters use put_nowait (see
# blueprints/media.py and core/agent_dispatch.py), so ANY event — including
# milestones like 'done' — drops when the queue is full rather than blocking
# the producer; terminal completion is conveyed out-of-band via job["done"]
# (set BEFORE the done event is emitted), so the SSE consumer still terminates
# even if the 'done' frame itself is dropped.
_QUEUE_MAXSIZE = 1000

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


# Per-job cancel events keyed by job_id. Set by ``signal_cancel`` (from the
# /upload/<id>/cancel route on the web path) and observed by run_batch's
# worker loop. Mirrors agent/dispatch._cancel_events so cancellation works
# both for agent-path jobs (relay frame to agent) and web-path jobs (in-
# process Event the run_batch thread polls). Entries are created in
# ``register_job`` and removed in ``drop_job``.
_cancel_events: dict[str, threading.Event] = {}


def get_cancel_event(job_id: str) -> threading.Event | None:
    """Return the cancel Event for a job, or None if unknown.

    Tests + the upload-cancel route use this; the run_batch worker thread
    reaches the event via its registered job entry rather than going through
    the registry every iteration.
    """
    with _JOBS_LOCK:
        return _cancel_events.get(job_id)


def signal_cancel(job_id: str) -> bool:
    """Mark *job_id* as cancelled. Returns True if a matching job existed.

    Called by ``blueprints/upload.upload_cancel`` for web-path jobs. The
    run_batch worker thread checks the event before each row submission and
    short-circuits remaining rows with an ``error_type: cancelled`` event.
    Cancellation is best-effort cooperative — rows already mid-flight (e.g.
    a YouTube chunk upload partway through) complete normally.
    """
    with _JOBS_LOCK:
        evt = _cancel_events.get(job_id)
    if evt is None:
        return False
    evt.set()
    return True


def register_job(job_id: str) -> dict:
    _warn_if_multiworker()
    _ensure_reaper_running()
    job = {"queue": queue.Queue(maxsize=_QUEUE_MAXSIZE), "done": False, "finished_at": None}
    with _JOBS_LOCK:
        _jobs[job_id] = job
        _cancel_events[job_id] = threading.Event()
    return job


def drop_job(job_id: str) -> None:
    with _JOBS_LOCK:
        _jobs.pop(job_id, None)
        _cancel_events.pop(job_id, None)


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
# finish before giving up. YouTube records a result on every exit path, so this
# only caps a truly hung upload — it must exceed any realistic upload time. A
# 1.4 GB video on a slow/contended home upstream took ~31 min in the field and
# blew the old 30-min cap (Rock Email errored "no watch URL" 90s before YT
# finished), so the default is now 2h. Overridable via
# `upload.youtube_wait_timeout_seconds` in config.yaml.
_YT_WAIT_TIMEOUT_S = 120 * 60
_YT_POLL_INTERVAL_S = 1.0


def _resolve_youtube_watch_url(iso_date, emit_phase):
    """Block until this date's YouTube Video upload result is available, then
    return (watch_url, error). Only called when a YT Video upload is expected
    for the date. Returns ("", reason) if it failed/timed out. Reads the
    shared session.upload_results, written by record_result as rows finish.
    """
    timeout_s = (load_config().get("upload", {}) or {}).get(
        "youtube_wait_timeout_seconds", _YT_WAIT_TIMEOUT_S)
    deadline = time.time() + timeout_s
    emit_phase("waiting_for_youtube")
    while time.time() < deadline:
        # CONC-006: read the two-level dict under the same RLock record_result
        # writes through, so a concurrent setdefault()+assign can't expose a
        # half-updated inner dict to this poller.
        with session._lock:
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


# ---------- Phase δ: per-org platform soft mutex ----------

def _wait_for_platform_lock(
    org_id: int, platform: str, user_id: int,
    emit, row_id: int, date_iso: str,
    timeout_s: float = 30.0, poll_interval_s: float = 0.5,
) -> bool:
    """Acquire ``(org_id, platform)`` for ``user_id`` or wait up to
    ``timeout_s`` seconds for the current holder to release.

    Emits a single ``phase_change`` with ``phase="platform_lock_wait"``
    the first time we have to wait (so the dashboard can render "Waiting
    for another user's upload to finish"), then polls. Returns True if
    we ended up holding the lock; False on timeout. On False the caller
    should turn the row into a per-row error.

    No-op (returns True immediately) when ``org_id`` is falsy — this
    keeps backward-compatible call sites that don't know the org_id (the
    legacy whole-session runner and tests using the in-memory session
    state) from blocking on the lock they can't acquire.
    """
    if not org_id or not user_id:
        return True
    # Fast path: not held → we win.
    if platform_locks.try_acquire(org_id, platform, user_id):
        return True
    # Emit the wait-phase once so the UI can show a "Waiting…" message,
    # then poll until either we win or timeout fires.
    try:
        holder = platform_locks.current_holder(org_id, platform) or {}
        emit({
            "type": "phase_change",
            "row": row_id,
            "date": date_iso,
            "platform": platform,
            "phase": "platform_lock_wait",
            "blocked_by_user_id": holder.get("locked_by_user_id"),
        })
    except Exception:  # noqa: BLE001 — never fail dispatch because of an SSE drop
        # Not fatal; the dispatch keeps going. But log at debug so a
        # consistently-broken SSE handler is at least visible during triage.
        _log.debug(
            "platform_lock_wait emit failed (row=%s platform=%s)",
            row_id, platform, exc_info=True,
        )
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        time.sleep(max(0.05, poll_interval_s))
        if platform_locks.try_acquire(org_id, platform, user_id):
            return True
    return False


def _breaker_for(platform: str):
    """Return the per-platform circuit breaker, configured from config.yaml.

    Browser-automation platforms are the expensive ones (Chrome launch plus a
    multi-minute login timeout per call), so a broken session there is exactly
    the cascade the breaker exists to cut short. Thresholds are shared across
    platforms via ``upload.circuit_breaker`` but the breaker instances are
    per-platform so one broken integration never disables a healthy one.
    """
    cb_cfg = (load_config().get("upload", {}) or {}).get("circuit_breaker", {}) or {}
    return get_breaker(
        f"upload:{platform}",
        failure_threshold=int(cb_cfg.get("failure_threshold", 3)),
        recovery_timeout=float(cb_cfg.get("recovery_timeout_seconds", 120)),
    )


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

    # Circuit breaker: if this platform has already failed repeatedly in this
    # run, fail fast instead of (for Playwright uploaders) relaunching Chrome
    # and blocking on the login timeout for every remaining date.
    breaker = _breaker_for(platform)
    if not breaker.allow():
        _phase("circuit_open")
        return {
            "success": False, "skipped": False, "url": "", "scheduled_time": "",
            "error": (
                f"{platform} temporarily disabled after repeated failures in "
                "this run. If its session expired, re-connect it in Settings, "
                "then re-run — already-completed rows are skipped."
            ),
        }

    def _invoke():
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

    try:
        result = _invoke()
    except _INFRA_FAILURES:
        # Infra failure (broken session, network, unresponsive page): count it
        # toward opening the breaker, then re-raise so the runner's existing
        # handlers turn it into the usual per-row error / re-Connect message.
        breaker.record_failure()
        raise
    # Only a genuine SUCCESS heals the breaker. A plain result-dict failure is
    # neutral (likely a per-row data issue, not infra). A SKIP (element- or
    # idempotently-disabled) exercised none of the platform's infra, so it must
    # NOT close a breaker that an actually-broken integration just opened —
    # previously a skipped row masked a dead session for the next real row.
    if result is not None and result.get("success") and not result.get("skipped"):
        breaker.record_success()
    return result


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
            # A YT Video row that's element-DISABLED (it["skipped"]) produces
            # no watch URL, so it must NOT be "expected" — otherwise the date's
            # Rock Email waits on a result that never comes and then errors,
            # instead of falling back to entry.youtube_watch_url.
            expected[iso] = (rid not in skip_set) and not it.get("skipped", False)
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
    cancel_event: threading.Event | None = None,
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

    # MAINT-001: the web run_batch was nearly silent in logs (only emit/DB
    # failures) while the agent path logs every step — a failed web run was
    # almost untriageable. Log run start + per-row outcomes with session_id
    # correlation, mirroring agent/run_batch.
    logger.info(
        "run_batch start: session=%s dates=%d rows=%d skipped=%d max_workers=%d",
        session_id, len(set(dates)), len(batch_summary), len(skip_set), max_workers,
    )

    def emit_safe(payload):
        try:
            emit(payload)
        except Exception:
            logger.exception("batch emit failed")

    # Capture the dispatching org once — the per-platform threads below
    # each get their own ``override`` block since the thread-local
    # doesn't propagate across executor.submit boundaries.
    from core.org_context import effective_org_id as _eoi, override as _oc_override
    _captured_oid = _eoi()

    def _upload_one(idx, item):
        with _oc_override(_captured_oid):
            platform = item["platform"]
            row_id = f"{item['date']}_{platform}"
            # Cooperative cancel: check BEFORE acquiring any per-platform
            # resources (Chrome launch, YouTube OAuth refresh, etc). Already-
            # in-flight rows are allowed to finish — cancellation is best-effort
            # cooperative, not a hard kill. Mirrors agent/run_batch._run_one.
            if cancel_event is not None and cancel_event.is_set():
                emit_safe({"type": "error", "row": idx, "date": item["date"],
                           "platform": platform, "error_type": "cancelled",
                           "message": "job cancelled before dispatch"})
                # If this is the YouTube Video a Rock Email row is waiting on,
                # record a failure result so the waiter resolves immediately
                # instead of polling the full 30-min timeout (WEB-11).
                if platform == "YouTube Video":
                    session.record_result(
                        item.get("iso_date", item["date"]), "YouTube Video",
                        {"success": False, "error": "cancelled"})
                return idx, item, None
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
    # CONC-003: submit Rock Email rows LAST. An email row blocks in
    # _resolve_youtube_watch_url waiting on the same date's YouTube Video
    # result; if email waiters fill every worker slot before the YouTube rows
    # are picked up, the pool deadlocks (waiters hold all slots while the YT
    # futures sit queued). Submitting every non-email row first means each
    # YouTube Video row is started (or finished) before any waiter can take the
    # last slot — its dependency always has a worker, so it always progresses.
    # idx is the ORIGINAL enumerate index, preserved through the reorder, so
    # dashboard row identity (the "row" field on every event) is unchanged.
    submission_order = sorted(
        enumerate(batch_summary),
        key=lambda pair: pair[1].get("platform") == "Rock Email",
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for idx, item in submission_order:
            # Pre-submit cancel check: if cancel was signalled while we were
            # submitting earlier rows, short-circuit the rest of this loop
            # rather than queueing N more workers that'll each emit their
            # own cancelled error frame. Each worker also re-checks the
            # event before any expensive work so this is belt-and-suspenders.
            if cancel_event is not None and cancel_event.is_set():
                emit_safe({"type": "error", "row": idx, "date": item["date"],
                           "platform": item["platform"],
                           "error_type": "cancelled",
                           "message": "job cancelled before dispatch"})
                continue
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
            # When a YouTube Video row raises (infra/SessionExpired) it never
            # called record_result, so a Rock Email row waiting on it would
            # poll the full 30-min timeout. Record a failure result on any
            # exception so the waiter resolves immediately (WEB-11).
            def _signal_yt_failure(reason):
                if item.get("platform") == "YouTube Video":
                    session.record_result(
                        item.get("iso_date", item["date"]), "YouTube Video",
                        {"success": False, "error": reason})
            try:
                _, _, result = future.result()
            except SessionExpiredError:
                _signal_yt_failure("session expired")
                emit_safe({"type": "error", "row": idx, "date": item["date"],
                           "platform": item["platform"],
                           "message": (f"Session expired for {item['platform']}. Open Settings "
                                       "and click 'Connect' to re-authenticate, then retry.")})
                continue
            except Exception as exc:
                _signal_yt_failure(str(exc))
                emit_safe({"type": "error", "row": idx, "date": item["date"],
                           "platform": item["platform"], "message": str(exc)})
                continue

            iso_date = item.get("iso_date", item["date"])
            if result is None:
                # result is None for: cancel-before-dispatch, entry-not-found,
                # and idempotent skips (row already recorded success in a prior
                # run — these are in skip_set). In every case the appropriate
                # event was already emitted in _upload_one. Do NOT write a DB
                # row here: the old code recorded success=False/error="skipped"
                # for idempotent skips, stamping a spurious FAILURE on top of a
                # (date, platform) that already has a real success row — which
                # shows as a failed row in History and can read as "not done".
                # The existing success row is the source of truth; leave it.
                continue

            session.record_result(iso_date, item["platform"], result)
            logger.info(
                "run_batch row done: session=%s date=%s platform=%s success=%s%s",
                session_id, item["date"], item["platform"],
                bool(result.get("success")),
                "" if result.get("success") else
                f" error={result.get('error') or 'unknown'}",
            )
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
                # CONC-002: the skip_set was built once at batch start. Under a
                # concurrent run for the same session, a (date, platform) can
                # pass that check in both runs and reach here twice. Re-check
                # immediately before the write so a successful row isn't
                # duplicated in upload_history / the History view. (This keeps
                # the persistence idempotent; fully preventing the concurrent
                # double *upload* to the platform would need a per-(session,
                # date, platform) claim — the PerUserRunLock already blocks the
                # common same-user trigger.)
                if (result.get("success")
                        and _db.has_successful_upload(session_id, iso_date, item["platform"])):
                    logger.info(
                        "CONC-002: skipping duplicate upload_history write for "
                        "(%s, %s, %s) — already recorded success",
                        session_id, iso_date, item["platform"],
                    )
                    continue
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
