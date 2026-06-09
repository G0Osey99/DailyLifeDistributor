"""Agent-side orchestration: dispatches each (row, platform) to the bundled
uploaders. Copy-and-trim of core.upload_jobs.run_batch with the db.* calls
removed (server pre-applies idempotent skip; server records upload_history
from emitted success events).

B4: skeleton + parallel pool (real dispatch added in B5/B6).
B5: circuit breaker + email-after-YT ordering.
B6: real per-platform dispatch into bundled uploaders.
Phase 3: per-run YT state (no module-level mutation between runs),
         circuit_breaker.reset_prefix("upload:") at the start of each run
         (resets only the upload:* breakers, not unrelated ones like
         llm:title — see CONC-004), and a Rock-Email guard that mirrors
         core/upload_jobs._dispatch_upload — when YT was expected but
         returned no URL, error out instead of calling rock_schedule_email
         with a blank link.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from core import circuit_breaker
from core.circuit_breaker import get_breaker

try:
    from playwright.sync_api import TimeoutError as _PlaywrightTimeout  # type: ignore
except Exception:
    class _PlaywrightTimeout(Exception):  # type: ignore
        """Placeholder when Playwright isn't importable."""

from core.playwright_session import SessionExpiredError

# Infrastructure failures that count toward opening the breaker.
# Per-row data failures (missing file, bad title) should NOT trip it.
# Must mirror core/upload_jobs._INFRA_FAILURES — a Playwright session expiry
# is infra (re-launching Chrome won't help), so it has to trip the breaker;
# otherwise the agent burns the full login timeout on every remaining date.
_INFRA_FAILURES = (
    _PlaywrightTimeout,
    SessionExpiredError,
    ConnectionError,
    TimeoutError,
    OSError,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-run state — created fresh in run(), threaded through _run_one /
# _dispatch_upload so two sequential runs cannot leak _yt_done / _yt_url
# from one job into the next.
# ---------------------------------------------------------------------------


@dataclass
class _YtState:
    """Per-run YouTube-done signalling.

    Module-level state is wrong here: a second run() call would otherwise
    inherit the previous run's Events and watch URLs, so a Rock Email row
    in run B could immediately resolve _wait_yt against run A's result.
    """
    done: dict[int, threading.Event] = field(default_factory=dict)
    url: dict[int, str | None] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # How long a Rock Email row will block on its date's YouTube Video. YT
    # records a result on EVERY exit path (success/fail/skip/breaker), so the
    # wait normally resolves the moment YT finishes; this cap only guards
    # against a truly hung YT thread. It must therefore exceed any realistic
    # upload time — a 1.4 GB video on a slow/contended home upstream took
    # ~31 min in the field and blew the old 30-min cap, so Rock Email gave up
    # 90s before YT actually finished and errored "no watch URL". 2h is ample.
    wait_timeout: float = 7200.0

    def record(self, row_idx: int, watch_url: str | None) -> None:
        with self.lock:
            self.url[row_idx] = watch_url
            ev = self.done.setdefault(row_idx, threading.Event())
            ev.set()

    def wait(self, row_idx: int, timeout: float | None = None) -> str | None:
        with self.lock:
            ev = self.done.setdefault(row_idx, threading.Event())
        ev.wait(timeout=self.wait_timeout if timeout is None else timeout)
        return self.url.get(row_idx)


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

def _run_one(platform: str, row: dict, emit, paths: dict, cb_cfg: dict,
             yt_state: _YtState,
             cancel_event: "threading.Event | None" = None) -> None:
    # Visibility: without these lines, a task that errored inside
    # _dispatch_upload was invisible in the agent log — the only signal
    # was "no events flowing", which is indistinguishable from "still
    # working". INFO so they appear at the default level.
    _logger.info(
        "run_batch._run_one: platform=%s row_idx=%s iso=%s paths_keys=%s",
        platform, row.get("row_idx"), row.get("iso_date"),
        sorted((paths.get(row.get("iso_date"), {}) or {}).keys()),
    )
    # Check cancellation BEFORE acquiring any platform resources (browser
    # launch, OAuth refresh, etc). In-flight rows that already passed this
    # gate are allowed to finish — cancellation is best-effort cooperative,
    # not a hard kill.
    if cancel_event is not None and cancel_event.is_set():
        emit({"type": "event", "event": "error", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error_type": "cancelled",
              "error": "job cancelled before dispatch"})
        if platform == "YouTube Video":
            # Wake any Rock Email row blocked on this date's YouTube result.
            yt_state.record(row["row_idx"], None)
        return

    breaker = _breaker_for(platform, cb_cfg)
    if not breaker.allow():
        emit({"type": "event", "event": "error", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error": "circuit_breaker_open"})
        if platform == "YouTube Video":
            # Critical: a Rock Email row for this date may already be blocked
            # in yt_state.wait(). Without recording None here it would hang the
            # full 30-min timeout holding a worker slot. Every other YT exit
            # path records; the breaker-open path must too.
            yt_state.record(row["row_idx"], None)
        return

    # Rock Email must wait for this date's YouTube Video result (if present).
    yt_expected = (platform == "Rock Email"
                   and "YouTube Video" in row.get("platforms", []))
    if yt_expected:
        _logger.info(
            "Rock Email (row %s): waiting for this date's YouTube Video to "
            "finish before posting", row["row_idx"])
        watch = yt_state.wait(row["row_idx"])
        _logger.info(
            "Rock Email (row %s): YouTube wait resolved — watch URL %s",
            row["row_idx"], "received" if watch else "MISSING (skip/fail/timeout)")
        row = dict(row)   # shallow copy so we don't mutate the shared row
        row["yt_watch_url"] = watch
        row["_yt_expected"] = True

    captured: list = []
    try:
        _dispatch_upload(platform=platform, row=row,
                         emit=_emit_capture(emit, captured), paths=paths)
        # Determine success from emitted frames.
        if any(f.get("event") == "success" for f in captured):
            breaker.record_success()
            if platform == "YouTube Video":
                # The bundled YouTube uploader returns the watch link under
                # result key "url"; the success-event payload is built from
                # result.items() (see _dispatch_upload), so the key is "url"
                # here too. Reading "watch_url" silently yielded None and
                # broke the same-date Rock-Email handoff on the agent path.
                url = next(
                    (f.get("payload", {}).get("url")
                     for f in captured if f.get("event") == "success"),
                    None,
                )
                yt_state.record(row["row_idx"], url)
        else:
            # No success event — treat as data failure (neutral to breaker).
            if platform == "YouTube Video":
                yt_state.record(row["row_idx"], None)

    except _INFRA_FAILURES as e:
        # exception() so the agent log carries the full traceback —
        # otherwise we get "error: timeout" with no clue what timed out
        # and no way to triage from a user's bug report.
        _logger.exception(
            "run_batch._run_one INFRA failure platform=%s row_idx=%s: %s",
            platform, row.get("row_idx"), e,
        )
        breaker.record_failure()
        emit({"type": "event", "event": "error", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error": f"{type(e).__name__}: {e}"})
        if platform == "YouTube Video":
            yt_state.record(row["row_idx"], None)

    except Exception as e:
        _logger.exception(
            "run_batch._run_one DATA failure platform=%s row_idx=%s: %s",
            platform, row.get("row_idx"), e,
        )
        # Data failure — neutral to breaker.
        emit({"type": "event", "event": "error", "platform": platform,
              "row_idx": row["row_idx"], "iso_date": row["iso_date"],
              "error": f"{type(e).__name__}: {e}"})
        if platform == "YouTube Video":
            yt_state.record(row["row_idx"], None)
    else:
        ok = any(f.get("event") == "success" for f in captured)
        # When an uploader returns {"success": False, ...} (a clean DATA
        # failure, no exception) the reason is only in the emitted error frame
        # — surface it in the log too, or a failed row is invisible here
        # (just "success=False") and undiagnosable without the dashboard.
        err = None if ok else next(
            (f.get("error") for f in captured if f.get("event") == "error"), None)
        _logger.info(
            "run_batch._run_one finished platform=%s row_idx=%s emits=%d success=%s%s",
            platform, row.get("row_idx"), len(captured), ok,
            f" error={err!r}" if err else "",
        )


# ---------------------------------------------------------------------------
# run() — public entry point
# ---------------------------------------------------------------------------

def run(*, envelope: dict, paths: dict, emit,
        cancel_event: "threading.Event | None" = None) -> None:
    """Execute the job plan. `paths` is {iso_date: {kind: local_path}}.
    `emit` is called once per event frame (dict).

    *cancel_event* (optional): a threading.Event the dispatcher sets when
    a ``cancel_job`` frame arrives from the server. Each pending task
    checks it before dispatching; in-flight tasks finish normally. New
    rows past the gate emit an ``error`` event with ``error_type:
    cancelled``. Backward compatible — callers that pass nothing get
    today's behaviour unchanged.

    Phase 3:
      - Per-run _YtState (no module-level mutation between calls).
      - circuit_breaker.reset_prefix("upload:") at the top so an upload
        breaker tripped by a previous run doesn't open-circuit the new one.
        The registry is process-global; per-run resets are a safe default for
        a single-agent fleet where the operator may have fixed the broken
        session between runs. Scoped to "upload:" so non-run breakers
        (e.g. llm:title) survive — see CONC-004.
    """
    # Drop the upload:* breakers tripped by a previous run on this process,
    # so a fixed session isn't open-circuited by stale state (CONC-004). Scope
    # to "upload:" rather than reset_all() so a non-run breaker like
    # "llm:title" — not per-run state — isn't wiped out from under another
    # component.
    circuit_breaker.reset_prefix("upload:")

    # Run the Playwright uploaders headless by default on the agent. It's a
    # background process on the user's machine with cached sessions — a Chrome
    # window popping up (and the macOS "control Chrome" prompt) is unwanted.
    # First-ever login is always headed regardless (core/playwright_session),
    # so this only affects the valid-session path. setdefault so an operator
    # can still force headed for debugging (e.g. SIMPLECAST_HEADLESS=false).
    import os as _os
    for _hl in ("SIMPLECAST_HEADLESS", "VISTA_SOCIAL_HEADLESS", "ROCK_HEADLESS"):
        _os.environ.setdefault(_hl, "true")

    # Guarantee a usable browser before any uploader launches. Normally the
    # startup prewarm has already finished, so this is an instant no-op; on a
    # first job that beats the prewarm it blocks once on the ~150 MB download.
    # Best-effort — a failed download leaves DLD_USE_BUNDLED_CHROMIUM unset and
    # the uploaders fall back to system Chrome (channel='chrome').
    try:
        from agent import chromium as _chromium
        _chromium.ensure_chromium(progress=lambda m: _logger.info("chromium: %s", m))
    except Exception:
        _logger.debug("chromium ensure raised; continuing", exc_info=True)

    rows = envelope["rows"]
    config = envelope.get("config", {})
    max_workers = int(config.get("max_workers", 4))
    cb_cfg: dict = config.get("circuit_breaker", {}) or {}

    yt_state = _YtState()
    # Honor a configured YouTube-wait timeout if the server supplied one
    # (config.youtube_wait_timeout_seconds), else keep the generous default.
    try:
        _wt = config.get("youtube_wait_timeout_seconds")
        if _wt:
            yt_state.wait_timeout = float(_wt)
    except (TypeError, ValueError):
        pass

    # Propagate circuit-breaker config into each row (picked up by _run_one).
    if cb_cfg:
        rows = [dict(r, _config_circuit_breaker=cb_cfg) for r in rows]

    tasks = []
    for row in rows:
        for platform in row["platforms"]:
            tasks.append((platform, row))
    # Submit Rock Email rows LAST. An email row blocks in yt_state.wait()
    # until its date's YouTube Video records a result; if email rows were
    # submitted first they could occupy every worker in the bounded pool,
    # leaving none free to actually run the YouTube Video they're waiting on
    # (the date's email then times out and posts without a link). Mirrors the
    # web path's CONC-003. Stable sort: False(0) before True(1).
    tasks.sort(key=lambda pt: pt[0] == "Rock Email")

    # Visibility: when a user reports "the agent did nothing", the absence
    # of this line tells us the dispatch never reached the executor — vs.
    # the breaker / uploader layer if the line IS there but no per-row
    # events follow.
    _logger.info(
        "run_batch: starting job=%s rows=%d tasks=%d max_workers=%d",
        (envelope.get("job_id") or "?")[:8],
        len(rows), len(tasks), max_workers,
    )

    # If nothing resolved for ANY row, the most likely cause is that the agent
    # has no media folders configured — in which case every file-backed task
    # would fail with its own generic file-not-found (N confusing errors for
    # one root cause). Emit a single actionable diagnostic up front. We still
    # PROCEED with the run: Rock (Daily Experience) and Rock Email can succeed
    # with no local media file (they use the server-supplied Wistia ref +
    # gathered image), so we must not short-circuit them.
    if tasks and not any((paths.get(r["iso_date"]) or {}) for _p, r in tasks):
        _logger.warning("run_batch: no media resolved for any row — "
                        "agent may have no media folders configured")
        emit({"type": "event", "event": "warning",
              "job_id": envelope.get("job_id"),
              "message": ("No local media matched for any selected date. If "
                          "this device's uploads fail file-not-found, open the "
                          "agent window → 'Configure media folders…' (or "
                          "auto-detect from a parent folder), then re-run.")})

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [
            ex.submit(_run_one, platform, row, emit, paths,
                      row.get("_config_circuit_breaker") or cb_cfg,
                      yt_state, cancel_event)
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
    # JSON has no datetime type, so the schedule datetimes cross the job
    # envelope as ISO strings. Parse them back to datetime (matching
    # ReviewEntry.from_dict) — otherwise the uploaders call
    # schedule_dt.strftime(...) on a str and die with
    # "'str' object has no attribute 'strftime'" (Vista/SimpleCast/YouTube
    # scheduling). Covers every datetime field via _DATETIME_FIELDS.
    from datetime import datetime as _dt
    for _f in getattr(ReviewEntry, "_DATETIME_FIELDS", ()):
        v = entry_data.get(_f)
        if isinstance(v, str) and v:
            try:
                entry_data[_f] = _dt.fromisoformat(v)
            except ValueError:
                entry_data[_f] = None
    # Drop any keys not in ReviewEntry's dataclass fields to avoid TypeError.
    known = set(ReviewEntry.__dataclass_fields__.keys())
    filtered = {k: v for k, v in entry_data.items() if k in known}
    return ReviewEntry(**filtered)


def _rehydrate_rock_image(payload: "dict | None"):
    """Rebuild a ``GatheredImage`` from the server-shipped Rock image payload.

    The server (which has the LLM) gathered the Vista background image and
    base64-encoded its bytes into the job plan. Write them to a temp file and
    return a GatheredImage so the orchestrator treats it exactly like a local
    gather. Returns None when no image was shipped or the payload is malformed
    (the orchestrator then falls back to its own gather, which surfaces the
    usual error on a machine without an LLM).
    """
    if not isinstance(payload, dict) or not payload.get("image_b64"):
        return None
    try:
        import base64 as _b64
        import tempfile as _tf
        from core.image_gatherer import GatheredImage
        data = _b64.b64decode(payload["image_b64"])
        fd, path = _tf.mkstemp(prefix="rock_bg_", suffix=".jpg")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return GatheredImage(
            file_path=path,
            photo_id=str(payload.get("photo_id", "")),
            source=str(payload.get("source", "")),
            topic=str(payload.get("topic", "")),
            photographer=str(payload.get("photographer", "")),
            photo_url=str(payload.get("photo_url", "")),
        )
    except Exception:
        _logger.warning("Rock: failed to rehydrate server-gathered image", exc_info=True)
        return None


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

    # Backfill the Wistia ref from the agent-resolved Shorts filename. On the
    # agent path the SERVER builds the entry with no media (files live here),
    # so its build_entry can't infer wistia_ref from the Shorts name and leaves
    # it "" — which makes Rock's Spotlight fail pre-flight with
    # "Rock can't run — missing: wistia_ref". The agent DOES have the Shorts
    # file, so infer it here (the ref is the "app YYMMDD" label from the name).
    if not getattr(e, "wistia_ref", ""):
        _short = p.get("short_video")
        if _short:
            from core.session_state import infer_wistia_ref
            e.wistia_ref = infer_wistia_ref(_short)

    if platform == "YouTube Video":
        e.youtube_video_path = p.get("video")
        e.thumbnail_path = p.get("thumbnail")

        def _progress_cb(percent, bytes_sent, bytes_total, eta_seconds):
            emit({"type": "event", "event": "upload_progress", "platform": platform,
                  "row_idx": row["row_idx"], "iso_date": iso,
                  "percent": percent, "bytes_sent": bytes_sent,
                  "bytes_total": bytes_total, "eta_seconds": eta_seconds})

        def _event_cb(payload):
            # Forward the uploader's processing-phase events (phase_change,
            # processing_start/done) so the dashboard sees the same signals
            # on the agent path as the web path (which passes event_callback).
            payload.setdefault("platform", platform)
            payload.setdefault("row_idx", row["row_idx"])
            payload.setdefault("iso_date", iso)
            emit(payload)

        result = youtube_uploader.upload_video(
            e, is_short=False, elements=elements,
            progress_callback=_progress_cb, event_callback=_event_cb,
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
        # The server pre-gathers the Vista background image (the agent has no
        # local LLM for the image gatherer) and ships it in the row. Rehydrate
        # it into a GatheredImage so the orchestrator uses it verbatim.
        pregathered = _rehydrate_rock_image(row.get("rock_image"))
        result = rock_orch.upload_daily_experience(
            e, elements=elements, pregathered_image=pregathered)

    elif platform == "Rock Email":
        e.email_thumbnail_path = p.get("email_thumbnail")
        # Watch URL: prefer the resolved value from the YT wait, then the
        # entry field, then empty string.
        watch_url = (row.get("yt_watch_url")
                     or getattr(e, "youtube_watch_url", None)
                     or "")
        # Mirror core/upload_jobs._dispatch_upload: when YT was expected for
        # this date but didn't return a URL, abort instead of calling
        # rock_schedule_email with a blank link (which would produce a draft
        # email pointing at nothing).
        if row.get("_yt_expected") and not watch_url:
            result = {
                "success": False,
                "error": ("YouTube Video upload did not produce a watch URL "
                          "for this date; cannot schedule the Daily Life email."),
            }
        else:
            result = rock_schedule_email(e, youtube_watch_url=watch_url,
                                         elements=elements)

    elif platform == "Vista Social":
        # Vista posts the SHORTS clip (uploader reads youtube_shorts_path),
        # not the horizontal video. The previous code set youtube_video_path,
        # so the Vista uploader always saw youtube_shorts_path=None and failed
        # file-not-found on the agent path. Match the uploader + the web path.
        e.youtube_shorts_path = p.get("short_video")
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
