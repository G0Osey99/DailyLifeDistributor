"""Browser-streaming media pipeline endpoints.

The browser drives the whole run: it uploads a spreadsheet (cached per
browser session), maps its columns, sends filenames for date matching, then
chunk-uploads each batch's media and triggers a server batch-run. All routes
sit behind the app's global auth gate.

This module is built up across plan tasks:
  * Task 5 — spreadsheet upload + column mapping (this file's first slice).
  * Task 6 — chunked upload + reassembly.
  * Task 8 — batch-run route + per-batch delete + run lifecycle.
  * Task 9 — /media/scan filename→date matching.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid

from flask import Blueprint, current_app, jsonify, request, session as flask_session

from core import media_session as ms
from core import upload_jobs
from core.excel_parser import (
    get_column_names,
    get_sheet_names,
    get_sheet_preview,
    parse_spreadsheet,
)
from core.file_scanner import parse_names
from core.session_state import session

bp = Blueprint("media", __name__)
_log = logging.getLogger(__name__)

# Spreadsheets are small (planning sheets); keep a tight cap.
_MAX_SPREADSHEET_BYTES = 5 * 1024 * 1024  # 5 MB

# ~95 MB keeps each chunk POST under Cloudflare's ~100 MB proxied-body cap.
_MAX_CHUNK = 95 * 1024 * 1024

# Hard ceiling on bytes concurrently on disk for a run, independent of the
# free-space check — bounds transient disk on the tight VPS even if df is
# momentarily generous. The counter is decremented as each batch's temp files
# are deleted, so in practice this caps a single batch's footprint, not a run's
# cumulative total. Overridable via DLD_MAX_RUN_BYTES.
_MAX_RUN_BYTES = int(os.environ.get("DLD_MAX_RUN_BYTES", str(40 * 1024 * 1024 * 1024)))

# Multi-tenant phase δ: per-user upload run lock so two users in the same
# (or different) org can run web uploads concurrently. The same user still
# gets one run at a time. `_runs` maps an active run_id to its RunDir +
# per-file reassembly state (still process-global because run_ids are unique).
_run_lock = ms.PerUserRunLock()
_runs: dict[str, dict] = {}
_runs_guard = threading.Lock()


def _session_user_id() -> int:
    """Return the logged-in user_id from the Flask session, or 0 for
    legacy single-tenant boots where /media/* runs before auth is wired.

    A non-int user_id can't occur via the auth blueprint but might during
    early migration boots; coerce defensively so the lock dict's keys
    stay typed.
    """
    uid = flask_session.get("user_id")
    try:
        return int(uid) if uid is not None else 0
    except (TypeError, ValueError):
        return 0


def _active_run(run_id: str) -> dict | None:
    if not run_id:
        return None
    with _runs_guard:
        return _runs.get(run_id)


def active_run_ids() -> set:
    """Run-ids with a live temp dir — passed to the orphan sweep so it never
    deletes an in-flight run's files."""
    with _runs_guard:
        return set(_runs.keys())


def _spreadsheet_dir() -> str:
    d = os.path.join(ms._TEMP_ROOT, "spreadsheets")
    os.makedirs(d, exist_ok=True)
    return d


def _media_sid() -> str:
    """Stable per-browser-session id used to namespace the cached spreadsheet.

    Flask's signed-cookie session has no server-side id, so we mint one and
    keep it in the session cookie.
    """
    sid = flask_session.get("media_sid")
    if not sid:
        sid = uuid.uuid4().hex
        flask_session["media_sid"] = sid
    return sid


def _spreadsheet_path() -> str:
    return os.path.join(_spreadsheet_dir(), _media_sid() + ".xlsx")


@bp.route("/media/spreadsheet", methods=["POST"])
def upload_spreadsheet():
    """Cache the uploaded .xlsx for this session and return its sheet names."""
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "No file provided"}), 400
    data = f.read(_MAX_SPREADSHEET_BYTES + 1)
    if len(data) > _MAX_SPREADSHEET_BYTES:
        return jsonify({"error": "Spreadsheet too large (max 5 MB)"}), 413
    path = _spreadsheet_path()
    with open(path, "wb") as out:
        out.write(data)
    sheets = get_sheet_names(path)
    if not sheets:
        # Unreadable / not a real xlsx — drop the bad cache file.
        try:
            os.unlink(path)
        except OSError:
            pass
        return jsonify({"error": "Could not read spreadsheet"}), 400
    return jsonify({"sheets": sheets})


@bp.route("/media/spreadsheet/columns")
def spreadsheet_columns():
    """Return the column headers + a short row preview for a sheet.

    The preview (first few rows, keyed by column name) lets the user eyeball
    which column holds what before mapping — the dropdowns alone don't show
    any sample data.
    """
    sheet = request.args.get("sheet", "")
    path = _spreadsheet_path()
    if not sheet or not os.path.isfile(path):
        return jsonify({"columns": [], "preview": []}), 400
    return jsonify({
        "columns": get_column_names(path, sheet),
        "preview": get_sheet_preview(path, sheet),
    })


@bp.route("/media/mapping", methods=["GET", "POST"])
def mapping():
    """Persist (POST) or return (GET) the per-session column mapping."""
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON object"}), 400
        flask_session["excel_mapping"] = data
        return jsonify({"success": True, "mapping": data})
    return jsonify({"mapping": flask_session.get("excel_mapping", {})})


# ---------------------------------------------------------------------------
# Chunked upload (Task 6). The browser slices each physical file into ≤95 MB
# chunks and POSTs them sequentially; the server appends them into a per-run
# temp file and reports completion on the last chunk.
# ---------------------------------------------------------------------------


@bp.route("/media/run/init", methods=["POST"])
def run_init():
    """Acquire the per-user run lock + allocate a temp dir. 409 if the
    *current user* already has a run active. Other users' runs do not
    block (multi-tenant phase δ)."""
    uid = _session_user_id()
    if _run_lock.holder(uid) is not None:
        return jsonify({"error": "An upload is already running"}), 409
    data = request.get_json(silent=True) or {}
    try:
        total_bytes = int(data.get("total_bytes") or 0)
    except (TypeError, ValueError):
        total_bytes = 0
    if total_bytes and not ms.has_free_space(total_bytes):
        return jsonify({"error": "Not enough free disk space for this run"}), 507
    # Phase δ admission control: refuse new web runs when the VPS volume
    # is below the minimum-free threshold (default 5 GiB,
    # overridable via DLD_DISK_MIN_FREE_BYTES). Agent-path uploads stream
    # from the user's own machine, so the message points users there.
    if not ms.has_minimum_free_space():
        return jsonify({
            "error": "VPS storage full; please use the agent path.",
        }), 507
    run = ms.RunDir.allocate()
    if not _run_lock.acquire(uid, run.run_id):
        run.cleanup()
        return jsonify({"error": "An upload is already running"}), 409
    with _runs_guard:
        _runs[run.run_id] = {"dir": run, "files": {}, "bytes_total": 0,
                             "user_id": uid}
    return jsonify({"run_id": run.run_id})


@bp.route("/media/file/new", methods=["POST"])
def file_new():
    """Issue an opaque server-side file-id for one physical file in the run."""
    run_id = request.args.get("run_id") or (request.get_json(silent=True) or {}).get("run_id", "")
    rec = _active_run(run_id)
    if rec is None:
        return jsonify({"error": "No active run"}), 409
    fid = rec["dir"].new_file_id()
    with _runs_guard:
        rec["files"][fid] = {"next": 0, "total": None, "bytes": 0, "complete": False}
    return jsonify({"file_id": fid})


@bp.route("/media/upload/chunk", methods=["POST"])
def upload_chunk():
    """Append one ordered chunk to its temp file; report completion on the last."""
    run_id = request.form.get("run_id", "")
    file_id = request.form.get("file_id", "")
    rec = _active_run(run_id)
    if rec is None:
        return jsonify({"error": "No active run"}), 409
    fstate = rec["files"].get(file_id)
    if fstate is None:
        return jsonify({"error": "Unknown file_id"}), 400
    try:
        chunk_index = int(request.form.get("chunk_index"))
        total_chunks = int(request.form.get("total_chunks"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad chunk_index/total_chunks"}), 400
    if total_chunks < 1 or chunk_index < 0 or chunk_index >= total_chunks:
        return jsonify({"error": "chunk index out of range"}), 400

    blob = request.files.get("data")
    if blob is None:
        return jsonify({"error": "no data"}), 400
    payload = blob.read(_MAX_CHUNK + 1)
    if len(payload) > _MAX_CHUNK:
        return jsonify({"error": "chunk too large"}), 413

    try:
        path = rec["dir"].file_path(file_id)  # also re-validates the file-id
    except ValueError:
        return jsonify({"error": "bad file_id"}), 400

    # Append strictly in order. A re-sent earlier chunk is acked idempotently;
    # a forward gap is refused so we never write a hole into the file.
    if chunk_index != fstate["next"]:
        if chunk_index < fstate["next"]:
            return jsonify({"ok": True, "duplicate": True})
        return jsonify({"error": "out-of-order chunk"}), 409

    # Disk-fill guards (defense in depth — these endpoints are auth-gated, but
    # the VPS volume is small). Reject before writing if this chunk would
    # breach the per-run ceiling or leave too little free space.
    if rec.get("bytes_total", 0) + len(payload) > _MAX_RUN_BYTES:
        return jsonify({"error": "Per-run upload size limit exceeded"}), 413
    if not ms.has_free_space(len(payload)):
        return jsonify({"error": "Not enough free disk space"}), 507

    with open(path, "wb" if chunk_index == 0 else "ab") as fh:
        fh.write(payload)
    rec["bytes_total"] = rec.get("bytes_total", 0) + len(payload)
    fstate["next"] = chunk_index + 1
    fstate["total"] = total_chunks
    fstate["bytes"] += len(payload)

    if fstate["next"] >= total_chunks:
        fstate["complete"] = True
        return jsonify({"complete": True, "bytes": fstate["bytes"]})
    return jsonify({"complete": False, "received": fstate["next"]})


# ---------------------------------------------------------------------------
# Batch run + lifecycle (Task 8). The browser, having chunk-uploaded all of a
# batch's files, asks the server to run the uploaders against the reassembled
# temp files, stream progress over the existing /upload/stream SSE, then delete
# the batch's temp files.
# ---------------------------------------------------------------------------


def _release_run(run_id: str) -> None:
    """Release the per-user run lock and remove + clean its temp dir."""
    with _runs_guard:
        rec = _runs.pop(run_id, None)
    if rec is not None:
        rec["dir"].cleanup()
        uid = int(rec.get("user_id") or 0)
    else:
        # The lock might still be held even if the runs dict has no record
        # (defensive: stale release / race during shutdown). Reverse-lookup
        # the user_id so we can still release the per-user slot.
        uid = _run_lock.user_for_run(run_id) or 0
    _run_lock.release(uid, run_id)


def _run_batch_worker(job_id, run_id, dates, summary, file_paths,
                      entries_snapshot, session_id, app, *, org_id=None):
    """Background worker: run one batch, stream events, delete its temp files.

    *org_id* is the effective org resolved at request time. The worker
    runs in a thread without a Flask request context, so
    ``effective_org_id()`` cannot recover it from ``flask.session``;
    threading-local override (``core.org_context.override``) propagates
    it to every credential read inside this worker (the uploader
    thread pool inherits it because ``override`` is thread-local on
    each spawned thread — see the explicit override inside the inner
    executor in core.upload_jobs.run_batch).
    """
    from core import org_context as _oc
    job = upload_jobs.get_job(job_id)
    q = job["queue"] if job else None
    # Cooperative cancel: register_job created an Event keyed by job_id.
    # Forward it into run_batch so POST /upload/<id>/cancel short-circuits
    # remaining row dispatches in this worker thread.
    cancel_event = upload_jobs.get_cancel_event(job_id)

    def emit(payload):
        if q is not None:
            try:
                q.put(json.dumps(payload))
            except Exception as e:  # noqa: BLE001 — a dropped event must not kill the worker
                _log.debug("media: dropped SSE event for run %s: %s", run_id, e)

    consumed: set = set()
    try:
        with app.app_context(), _oc.override(org_id):
            consumed = upload_jobs.run_batch(
                dates=dates, summary=summary, file_paths=file_paths,
                session_id=session_id, emit=emit, entries_snapshot=entries_snapshot,
                cancel_event=cancel_event,
            ) or set()
    except Exception as exc:  # noqa: BLE001 — surface to the SSE consumer
        emit({"type": "error", "message": f"Batch run crashed: {exc}"})
    finally:
        # Per-batch delete: every physical temp file this batch used, once.
        # Track freed bytes so the run's byte counter reflects what's actually
        # on disk — files are removed each batch, so the _MAX_RUN_BYTES ceiling
        # bounds *concurrent* (per-batch) usage, not a run's cumulative total.
        freed = 0
        for path in set(file_paths.values()) | consumed:
            try:
                freed += os.path.getsize(path)
            except OSError:
                pass
            try:
                os.remove(path)
            except OSError:
                pass
        rec = _active_run(run_id)
        if rec is not None:
            rec["bytes_total"] = max(0, rec.get("bytes_total", 0) - freed)
        emit({"type": "batch_done", "run_id": run_id})
        emit({"type": "done"})
        if job is not None:
            job["done"] = True
            job["finished_at"] = time.time()


@bp.route("/media/batch/run", methods=["POST"])
def batch_run():
    """Validate the batch's files are reassembled, then run + stream + delete.

    Body JSON:
      run_id   — the active run
      dates    — iso dates in this batch
      platforms— enabled platform toggle keys (youtube_video, ...)
      files    — {file_id: {"category": str, "date": iso}}
    Returns {job_id}; the browser consumes /upload/stream?job_id=...
    """
    data = request.get_json(silent=True) or {}
    run_id = data.get("run_id", "")
    rec = _active_run(run_id)
    if rec is None:
        return jsonify({"error": "No active run"}), 409

    dates = list(data.get("dates") or [])
    platforms = list(data.get("platforms") or [])
    files = data.get("files") or {}
    # Per-date user edits from the customize step: {iso: {field: value}}.
    overrides = data.get("overrides") or {}

    # Reassembly handshake: every declared file-id must be fully received.
    # A shared physical file may map to several (category, date) placements,
    # so each file_id carries either one placement dict or a list of them.
    file_paths: dict = {}
    for file_id, placements in files.items():
        fstate = rec["files"].get(file_id)
        if fstate is None or not fstate.get("complete"):
            return jsonify({
                "error": f"File {file_id} is not fully uploaded yet",
            }), 409
        try:
            path = rec["dir"].file_path(file_id)
        except ValueError:
            return jsonify({"error": "bad file_id"}), 400
        if isinstance(placements, dict):
            placements = [placements]
        for pl in placements or []:
            category = (pl or {}).get("category", "")
            iso = (pl or {}).get("date", "")
            if category and iso:
                file_paths[(category, iso)] = path

    # Rebuild this batch's session entries from the cached spreadsheet + the
    # temp files (titles/descriptions/tags/Rock fields/schedules), then select
    # the batch's dates/platforms so get_summary() yields the right rows.
    _apply_paths_to_session(file_paths, dates, platforms, overrides)
    summary = [it for it in session.get_summary()
               if it.get("iso_date", it.get("date")) in set(dates)]
    entries_snapshot = dict(session.entries)

    job_id = str(uuid.uuid4())
    upload_jobs.reap_stale_jobs()
    job = upload_jobs.register_job(job_id)

    # Phase 3: route to the local agent if the dashboard chose that path.
    use_agent = (
        request.args.get("path") == "agent"
        and os.environ.get("HYBRID_AGENT_ENABLED", "").lower() == "true"
    )
    if use_agent:
        from core import agent_dispatch
        from core.config import load_config as _load_config
        _cfg = _load_config()
        _max_workers = (_cfg.get("upload") or {}).get("max_workers", 4)
        # Phase 3.5 — accept an explicit device picker selection from the
        # dashboard. The browser passes ?device_id=<uuid> when the user
        # has chosen a specific device; absent → fallback chain runs.
        # Also pass the browser's _client_ip() so the dispatch can compute
        # same-network when no explicit pick was made.
        _picked_device = (request.args.get("device_id")
                          or (data.get("device_id") if isinstance(data, dict) else None)
                          or None)
        _browser_ip = None
        try:
            from blueprints.agent import _client_ip as _bp_client_ip
            _browser_ip = _bp_client_ip()
        except Exception:  # noqa: BLE001 — never block the upload on this
            _browser_ip = None
        try:
            # IMPORTANT: pass the pre-generated job_id (already in
            # upload_jobs._jobs) so /upload/stream can find it. Without
            # this start() mints its own uuid and the browser SSE 404s.
            job_id = agent_dispatch.start(
                session_id=session.session_id,
                summary=summary,
                entries=entries_snapshot,
                elements={
                    iso: entry.elements.to_dict()
                    for iso, entry in entries_snapshot.items()
                },
                config={"max_workers": _max_workers},
                device_id=_picked_device,
                browser_ip=_browser_ip,
                job_id=job_id,
            )
        except agent_dispatch.NoAgentOnlineError:
            upload_jobs.drop_job(job_id)
            # Release the run lock + clean the per-run temp dir: the agent
            # path doesn't run _run_batch_worker's finally block, so without
            # this the next /media/run/init returns 409 forever and the
            # batch's temp files leak on the VPS volume.
            _release_run(run_id)
            return jsonify({"error": "no_agent_online"}), 409
        agent_dispatch.register_job(job_id=job_id, sse_queue=job["queue"],
                                    session_id=session.session_id)
        # The agent streams media from the user's machine; the per-run temp
        # files the browser already uploaded are not consumed by the agent
        # path. Release the run lock + delete the temp files now so the
        # next run can start immediately and the VPS volume doesn't carry
        # the batch through the (possibly hours-long) agent upload.
        _release_run(run_id)
        return jsonify({"job_id": job_id})

    app_obj = current_app._get_current_object()  # type: ignore[attr-defined]
    # Capture the effective org at request time; the worker thread can't
    # read flask.session and needs this to honor impersonation when its
    # uploaders' credential reads call effective_org_id().
    from core.org_context import effective_org_id as _eoi
    captured_org_id = _eoi()
    try:
        threading.Thread(
            target=_run_batch_worker,
            args=(job_id, run_id, dates, summary, file_paths, entries_snapshot,
                  session.session_id, app_obj),
            kwargs={"org_id": captured_org_id},
            name=f"media-batch-{run_id[:8]}",
            daemon=True,
        ).start()
    except Exception as exc:  # noqa: BLE001 — never leave the run lock stuck
        # The worker (which would have released the lock) never ran; release
        # here so a failed thread start can't wedge the single-run lock forever.
        _release_run(run_id)
        return jsonify({"error": f"Could not start upload worker: {exc}"}), 500
    return jsonify({"job_id": job_id})


# Override keys the customize step may send, mapped to ReviewEntry fields.
# The set widened in the platform-tabs review refactor: each tab exposes
# its platform-unique field (episode_title for SimpleCast, vista_caption
# for Vista Social), and edits flow through here so the upload uses the
# user's text instead of the spreadsheet's value. Shared fields
# (description, podcast_title) are still single-keyed — multiple tabs
# can write the same key, last-write-wins per (date, key).
_OVERRIDE_FIELDS = {
    "youtube_title": "youtube_title",
    "youtube_shorts_title": "youtube_shorts_title",
    "podcast_title": "podcast_title",
    "episode_title": "episode_title",
    "vista_caption": "vista_caption",
    "description": "description",
}


def _apply_paths_to_session(file_paths, dates, platforms, overrides=None):
    """Rebuild this batch's session entries from the cached spreadsheet + temp
    files so uploads carry the mapped metadata — titles, descriptions, tags,
    Rock fields, transcript, and the per-platform schedule/element defaults —
    not blanks. Per-date `overrides` from the customize step (e.g. an
    auto-filled Shorts title) win over the spreadsheet values. The browser's
    platform selection for this batch is authoritative.
    """
    from core.file_scanner import MediaDateEntry
    overrides = overrides or {}

    plat_flags = {k: (k in platforms) for k in (
        "youtube_video", "youtube_shorts", "simplecast", "rock",
        "rock_email", "vista_social",
    )}

    # Per-date metadata from the cached spreadsheet under the session's mapping.
    meta_by_date: dict = {}
    sheet_path = _spreadsheet_path()
    mapping = flask_session.get("excel_mapping") or {}
    if os.path.isfile(sheet_path) and mapping.get("date_column"):
        try:
            meta_by_date = parse_spreadsheet(sheet_path, mapping)
        except Exception:  # noqa: BLE001 — a bad sheet shouldn't 500 the run
            meta_by_date = {}

    # Group this batch's temp files into a media-like object per date.
    media_by_date: dict = {}
    for (category, iso), path in file_paths.items():
        field = upload_jobs._CATEGORY_FIELD.get(category)
        if not field:
            continue
        m = media_by_date.setdefault(iso, MediaDateEntry(date=iso, display_date=iso))
        setattr(m, field, path)

    # build_entry() pulls titles/tags/schedules/elements from config + meta.
    with session._lock:
        for iso in dates:
            entry = session.build_entry(
                iso,
                media=media_by_date.get(iso),
                meta=meta_by_date.get(iso, {}),
                global_platforms=plat_flags,
            )
            for key, value in (overrides.get(iso) or {}).items():
                field = _OVERRIDE_FIELDS.get(key)
                if field and isinstance(value, str) and value.strip():
                    setattr(entry, field, value.strip())
            session.entries[iso] = entry
        session.selected_dates = list(dates)


@bp.route("/media/run/finish", methods=["POST"])
def run_finish():
    """Release the run lock and delete its temp dir (idempotent)."""
    data = request.get_json(silent=True) or {}
    run_id = data.get("run_id") or request.args.get("run_id", "")
    _release_run(run_id)
    return jsonify({"success": True})


@bp.route("/media/scan", methods=["POST"])
def scan():
    """Filename→date matching with sheet metadata (no filesystem access).

    Body JSON: ``{"categories": {"youtube_video": [names], "youtube_shorts":
    [...], "podcast": [...], "thumbnails": [...], "email_thumbnails": [...]}}``.
    Returns ``{"dates": {iso: {"categories": {cat: [names]}, "metadata":
    {...}}}}`` — matched filenames per category per date, plus the cached
    spreadsheet's metadata (title/transcript/...) for each matched date.

    Date matching is anchored to the loaded spreadsheet: when a sheet is
    present with a mapped date column, only filename-parsed dates that
    appear in the sheet are returned. This eliminates noise from old
    archive files (e.g. ``YYMMDD``-named clips from previous years that
    happen to also parse as a date in the user's target month) and
    disambiguates year-less filenames (``0601.jpg``, ``602.jpg``)
    against the user's actual scheduling intent. Without a sheet
    loaded, the full parse is returned (preserves manual workflows).
    """
    data = request.get_json(silent=True) or {}
    categories = data.get("categories") or {}

    dates: dict = {}
    for category, names in categories.items():
        if not isinstance(names, list):
            continue
        for iso, fnames in parse_names(names).items():
            slot = dates.setdefault(iso, {"categories": {}, "metadata": {}})
            bucket = slot["categories"].setdefault(category, [])
            for n in fnames:
                if n not in bucket:
                    bucket.append(n)

    # Attach the cached spreadsheet's per-date metadata where it's mapped.
    path = _spreadsheet_path()
    mapping = flask_session.get("excel_mapping") or {}
    sheet_dates: set[str] | None = None
    if os.path.isfile(path) and mapping.get("date_column"):
        try:
            meta_by_date = parse_spreadsheet(path, mapping)
        except Exception:  # noqa: BLE001 — a bad sheet shouldn't 500 the scan
            meta_by_date = {}
        for iso, meta in meta_by_date.items():
            if iso in dates:
                dates[iso]["metadata"] = meta
        sheet_dates = set(meta_by_date.keys())

    # Anchor to the sheet: drop parsed dates that aren't in the user's
    # scheduling plan. ``meta_by_date`` is empty when the sheet can't be
    # parsed — in that case we keep the full set so the scan still works
    # as a fallback. If the sheet is missing entirely (no date_column
    # mapping), we also skip the filter — that's the manual workflow
    # where the filenames are the source of truth.
    if sheet_dates:
        dates = {iso: payload for iso, payload in dates.items()
                 if iso in sheet_dates}

    return jsonify({"dates": dates})


@bp.route("/media/suggest-titles", methods=["POST"])
def suggest_titles():
    """LLM title suggestions from a transcript (the customize step's auto-fill).

    Stateless: the browser sends the date's transcript text (already returned
    by /media/scan), so this doesn't depend on the workflow session. Returns
    {suggestions: [...]} or an actionable error.
    """
    from core.llm_title_gen import generate_title_suggestions, is_llamafile_running

    data = request.get_json(silent=True) or {}
    transcript = (data.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"error": "No transcript for this date"}), 422
    if not is_llamafile_running():
        return jsonify({"error": "Title LLM backend is not reachable"}), 503
    # When the client omits `count`, fall through to the config's
    # ``llm.num_title_suggestions`` by passing ``num_suggestions=None``
    # to generate_title_suggestions. Previously this hardcoded 5 here,
    # ignoring the operator's config value (user reported getting 5
    # suggestions with config set to 3).
    raw_count = data.get("count")
    count: int | None
    if raw_count is None:
        count = None
    else:
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = None
        if count is not None:
            count = max(1, min(count, 10))
    try:
        suggestions = generate_title_suggestions(transcript, num_suggestions=count)
    except Exception as exc:  # noqa: BLE001
        # Surface the message in the JSON response AND log the stack
        # so ops can triage; without this we only ever see str(exc).
        _log.exception("title generation failed (count=%s)", count)
        return jsonify({"error": f"Title generation failed: {exc}"}), 500
    return jsonify({"suggestions": suggestions or []})
