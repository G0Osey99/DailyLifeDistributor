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
import os
import threading
import time
import uuid

from flask import Blueprint, current_app, jsonify, request, session as flask_session

from core import media_session as ms
from core import upload_jobs
from core.excel_parser import get_column_names, get_sheet_names, parse_spreadsheet
from core.file_scanner import parse_names
from core.session_state import session

bp = Blueprint("media", __name__)

# Spreadsheets are small (planning sheets); keep a tight cap.
_MAX_SPREADSHEET_BYTES = 5 * 1024 * 1024  # 5 MB

# ~95 MB keeps each chunk POST under Cloudflare's ~100 MB proxied-body cap.
_MAX_CHUNK = 95 * 1024 * 1024

# Hard ceiling on total bytes reassembled within one run, independent of the
# free-space check — bounds disk use on the tight VPS even if df is momentarily
# generous. Overridable via DLD_MAX_RUN_BYTES.
_MAX_RUN_BYTES = int(os.environ.get("DLD_MAX_RUN_BYTES", str(40 * 1024 * 1024 * 1024)))

# One upload run at a time across the process. The lock holder is the run_id;
# `_runs` maps an active run_id to its RunDir + per-file reassembly state.
_run_lock = ms.RunLock()
_runs: dict[str, dict] = {}
_runs_guard = threading.Lock()


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
    """Return the column headers for a sheet in the cached spreadsheet."""
    sheet = request.args.get("sheet", "")
    path = _spreadsheet_path()
    if not sheet or not os.path.isfile(path):
        return jsonify({"columns": []}), 400
    return jsonify({"columns": get_column_names(path, sheet)})


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
    """Acquire the single run lock + allocate a temp dir. 409 if one is active."""
    if _run_lock.holder() is not None:
        return jsonify({"error": "An upload is already running"}), 409
    data = request.get_json(silent=True) or {}
    try:
        total_bytes = int(data.get("total_bytes") or 0)
    except (TypeError, ValueError):
        total_bytes = 0
    if total_bytes and not ms.has_free_space(total_bytes):
        return jsonify({"error": "Not enough free disk space for this run"}), 507
    run = ms.RunDir.allocate()
    if not _run_lock.acquire(run.run_id):
        run.cleanup()
        return jsonify({"error": "An upload is already running"}), 409
    with _runs_guard:
        _runs[run.run_id] = {"dir": run, "files": {}, "bytes_total": 0}
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
    """Release the run lock and remove + clean its temp dir."""
    with _runs_guard:
        rec = _runs.pop(run_id, None)
    if rec is not None:
        rec["dir"].cleanup()
    _run_lock.release(run_id)


def _run_batch_worker(job_id, run_id, dates, summary, file_paths,
                      entries_snapshot, session_id, app):
    """Background worker: run one batch, stream events, delete its temp files."""
    job = upload_jobs.get_job(job_id)
    q = job["queue"] if job else None

    def emit(payload):
        if q is not None:
            try:
                q.put(json.dumps(payload))
            except Exception:
                pass

    consumed: set = set()
    try:
        with app.app_context():
            consumed = upload_jobs.run_batch(
                dates=dates, summary=summary, file_paths=file_paths,
                session_id=session_id, emit=emit, entries_snapshot=entries_snapshot,
            ) or set()
    except Exception as exc:  # noqa: BLE001 — surface to the SSE consumer
        emit({"type": "error", "message": f"Batch run crashed: {exc}"})
    finally:
        # Per-batch delete: every physical temp file this batch used, once.
        for path in set(file_paths.values()) | consumed:
            try:
                os.remove(path)
            except OSError:
                pass
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

    # Point this run's session entries at the temp files + select the batch's
    # dates/platforms so get_summary() yields the right rows.
    _apply_paths_to_session(file_paths, dates, platforms)
    session.selected_dates = list(dates)
    summary = [it for it in session.get_summary()
               if it.get("iso_date", it.get("date")) in set(dates)]
    entries_snapshot = dict(session.entries)

    job_id = str(uuid.uuid4())
    upload_jobs.reap_stale_jobs()
    upload_jobs.register_job(job_id)

    app_obj = current_app._get_current_object()  # type: ignore[attr-defined]
    threading.Thread(
        target=_run_batch_worker,
        args=(job_id, run_id, dates, summary, file_paths, entries_snapshot,
              session.session_id, app_obj),
        name=f"media-batch-{run_id[:8]}",
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


def _apply_paths_to_session(file_paths, dates, platforms):
    """Make sure each batch date has a session entry whose path fields point at
    the temp files and whose platform toggles match the selection."""
    from core.session_state import ReviewEntry
    from datetime import datetime
    plat_flags = {k: (k in platforms) for k in (
        "youtube_video", "youtube_shorts", "simplecast", "rock",
        "rock_email", "vista_social",
    )}
    for iso in dates:
        entry = session.entries.get(iso)
        if entry is None:
            try:
                display = datetime.strptime(iso, "%Y-%m-%d").strftime("%B %d, %Y")
            except ValueError:
                display = iso
            entry = ReviewEntry(date=iso, display_date=display)
            session.entries[iso] = entry
        entry.platforms_enabled.update(plat_flags)
    for (category, iso), path in file_paths.items():
        entry = session.entries.get(iso)
        field = upload_jobs._CATEGORY_FIELD.get(category)
        if entry is not None and field:
            setattr(entry, field, path)


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
    if os.path.isfile(path) and mapping.get("date_column"):
        try:
            meta_by_date = parse_spreadsheet(path, mapping)
        except Exception:  # noqa: BLE001 — a bad sheet shouldn't 500 the scan
            meta_by_date = {}
        for iso, meta in meta_by_date.items():
            if iso in dates:
                dates[iso]["metadata"] = meta

    return jsonify({"dates": dates})
