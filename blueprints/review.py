"""Per-date review, edit, rescan, and LLM title-suggestion routes."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from core.config import default_platforms, load_config
from core.excel_parser import ExcelParser
from core.file_scanner import FileScanner
from core.llm_title_gen import (
    clear_cache as clear_llm_cache,
    generate_title_suggestions,
    is_llamafile_running,
)
from core.session_state import ReviewEntry, session

bp = Blueprint("review", __name__)


def _build_entry_for_date(
    iso_date: str,
    platforms_enabled: dict | None = None,
    path_overrides: dict | None = None,
) -> ReviewEntry:
    config = load_config()
    scanner = FileScanner(config)

    current_app.logger.debug("_build_entry_for_date path_overrides: %s", path_overrides)
    if path_overrides:
        all_media = scanner.scan_custom_paths(path_overrides)
        media_entries = [m for m in all_media if m.date == iso_date]
    else:
        media_entries = scanner.get_files_for_dates([iso_date])
    media = media_entries[0] if media_entries else None
    current_app.logger.debug("_build_entry_for_date media found for %s: %s", iso_date, media is not None)
    meta = ExcelParser(config).get_metadata_for_date(iso_date) or {}

    if platforms_enabled is None:
        platforms_enabled = default_platforms(config)

    return session.build_entry(iso_date, media=media, meta=meta, global_platforms=platforms_enabled)


@bp.route("/review")
def review():
    if not session.entries:
        flash("No dates selected. Please select dates first.", "warning")
        return redirect(url_for("scan.index"))

    entries = [session.entries[d].to_dict() for d in session.selected_dates if d in session.entries]
    # Pass `entries` directly; the template uses `| tojson` which is
    # <script>-safe (escapes </ to <\/) — unlike json.dumps + |safe.
    return render_template("review.html", entries=entries)


@bp.route("/review/update", methods=["PATCH", "POST"])
def review_update():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Invalid JSON"}), 400

    date = data.get("date")
    field = data.get("field")
    value = data.get("value")

    if not date or not field:
        return jsonify({"success": False, "error": "Missing date or field"}), 400

    if field == "reinterpret_date":
        new_date = value
        try:
            datetime.strptime(new_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "value must be YYYY-MM-DD"}), 400
        # Hold the session lock while shuffling entries/selected_dates — the
        # upload worker pool reads both concurrently.
        with session._lock:
            if date in session.entries and new_date != date:
                entry = session.entries.pop(date)
                entry.date = new_date
                entry.display_date = datetime.strptime(new_date, "%Y-%m-%d").strftime("%B %d, %Y")
                session.entries[new_date] = entry
                session.upload_results.pop(date, None)
                session.selected_dates = [
                    new_date if d == date else d for d in session.selected_dates
                ]
        return jsonify({"success": True, "new_date": new_date})

    ok = session.update_entry(date, field, value)
    return jsonify({"success": ok})


@bp.route("/review/update-all", methods=["POST"])
def review_update_all():
    """Update a field across ALL dates in the current session."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Invalid JSON"}), 400

    field = data.get("field")
    value = data.get("value")

    if not field:
        return jsonify({"success": False, "error": "Missing field"}), 400

    count = session.update_all_entries(field, value)
    return jsonify({"success": True, "updated_count": count})


@bp.route("/rescan")
def rescan_date():
    """Re-scan scanner/docx metadata for a single date and refresh just that entry."""
    iso_date = (request.args.get("date") or "").strip()
    source_date = (request.args.get("source_date") or iso_date).strip()

    if not iso_date:
        return jsonify({"success": False, "error": "Missing date query parameter"}), 400

    try:
        datetime.strptime(iso_date, "%Y-%m-%d")
        datetime.strptime(source_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"success": False, "error": "Date must be YYYY-MM-DD"}), 400

    existing = session.entries.get(source_date) or session.entries.get(iso_date)
    existing_platforms = dict(existing.platforms_enabled) if existing else None

    current_app.logger.debug("Path overrides at rescan time: %s", session.path_overrides)
    refreshed = _build_entry_for_date(iso_date, existing_platforms, path_overrides=session.path_overrides)

    # Same race window as record_result: rescan can fire while the upload
    # pool is still draining the previous run.
    with session._lock:
        if source_date != iso_date:
            session.entries.pop(source_date, None)
            session.upload_results.pop(source_date, None)
            session.selected_dates = [iso_date if d == source_date else d for d in session.selected_dates]

        session.entries[iso_date] = refreshed
        session.upload_results.pop(iso_date, None)

        if iso_date not in session.selected_dates:
            session.selected_dates.append(iso_date)

        session.selected_dates = sorted(set(session.selected_dates), reverse=True)

    return jsonify({
        "success": True,
        "entry": refreshed.to_dict(),
        "source_date": source_date,
    })


# ---------------------------------------------------------------------------
# Async title-generation jobs.
#
# The LLM call takes 5-15s; if we do that work on the request thread the
# browser sees a long-pending POST and the Flask threadpool gets pinned.
# Instead /generate-titles spawns a worker thread, returns a job_id
# immediately, and the JS polls /generate-titles/status/<job_id> until the
# result is ready. The transcript text comes from the mapped spreadsheet
# column (no on-the-fly transcription).
#
# Same single-process caveat as core.upload_jobs: this dict is per-worker and
# will not work behind a multi-worker WSGI server. The launch script runs
# Flask single-process, so this is fine in production.
# ---------------------------------------------------------------------------

_log = logging.getLogger(__name__)

_title_jobs: dict[str, dict] = {}
_title_jobs_lock = threading.Lock()
_TITLE_JOB_RETENTION_SECONDS = 600  # 10 min after completion


def _reap_title_jobs() -> None:
    cutoff = time.time() - _TITLE_JOB_RETENTION_SECONDS
    with _title_jobs_lock:
        stale = [jid for jid, j in _title_jobs.items()
                 if j.get("finished_at") and j["finished_at"] < cutoff]
        for jid in stale:
            _title_jobs.pop(jid, None)


def _set_title_job(job_id: str, **fields) -> None:
    with _title_jobs_lock:
        existing = _title_jobs.setdefault(job_id, {})
        existing.update(fields)


def _run_title_generation(job_id: str, iso_date: str, app) -> None:
    """Worker: read the mapped transcript text + generate suggestions.

    The transcript comes from the spreadsheet's mapped transcript column
    (stored on the ReviewEntry, with a fallback to a freshly-parsed cached
    sheet). No audio transcription happens — Whisper was removed.
    """
    try:
        with app.app_context():
            entry = session.entries.get(iso_date)
            if not entry:
                _set_title_job(
                    job_id,
                    status="error",
                    http_status=404,
                    error=f"No entry found for date {iso_date}",
                    finished_at=time.time(),
                )
                return

            transcript_text = (getattr(entry, "transcript", "") or "").strip()
            if not transcript_text:
                # Fall back to the cached/configured sheet in case the entry
                # predates transcript population.
                config = load_config()
                meta = ExcelParser(config).get_metadata_for_date(iso_date) or {}
                transcript_text = (meta.get("transcript") or "").strip()
            transcript_source = "excel"

            if not transcript_text:
                _set_title_job(
                    job_id, status="error", http_status=422,
                    error=(
                        "No transcript for this date. Map a transcript column "
                        "on the dashboard so titles can be suggested."
                    ),
                    finished_at=time.time(),
                )
                return

            if not is_llamafile_running():
                _set_title_job(
                    job_id,
                    status="error",
                    http_status=503,
                    error="llamafile is not running. It should start automatically on launch.",
                    finished_at=time.time(),
                )
                return

            clear_llm_cache()
            suggestions = generate_title_suggestions(transcript_text, num_suggestions=5)
            entry.llm_title_suggestions = suggestions

            transcript_preview = (
                transcript_text[:300] + "..." if len(transcript_text) > 300 else transcript_text
            )
            _set_title_job(
                job_id,
                status="done",
                suggestions=suggestions,
                transcript_preview=transcript_preview,
                transcript_source=transcript_source,
                finished_at=time.time(),
            )
    except Exception as exc:  # noqa: BLE001 - surface any failure to the poller
        _log.exception("title generation job %s crashed", job_id)
        _set_title_job(
            job_id,
            status="error",
            http_status=500,
            error=str(exc) or exc.__class__.__name__,
            finished_at=time.time(),
        )


@bp.route("/generate-titles", methods=["POST"])
def generate_titles():
    """Kick off async LLM title generation from the transcript. Returns {job_id}."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    iso_date = data.get("date", "")
    if not iso_date or iso_date not in session.entries:
        # Fail fast on obviously-broken requests so the JS never has to poll
        # a job that can't possibly succeed.
        return jsonify({"error": f"No entry found for date {iso_date}"}), 404

    _reap_title_jobs()

    job_id = str(uuid.uuid4())
    _set_title_job(job_id, status="running", started_at=time.time())

    app_obj = current_app._get_current_object()  # type: ignore[attr-defined]
    threading.Thread(
        target=_run_title_generation,
        args=(job_id, iso_date, app_obj),
        name=f"title-gen-{iso_date}",
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "status": "running"}), 202


@bp.route("/generate-titles/status/<job_id>")
def generate_titles_status(job_id: str):
    """Poll endpoint for an in-flight title-generation job."""
    with _title_jobs_lock:
        job = _title_jobs.get(job_id)
        snapshot = dict(job) if job else None

    if snapshot is None:
        return jsonify({"status": "error", "error": "Job not found or expired"}), 404

    status = snapshot.get("status", "running")
    if status == "running":
        return jsonify({"status": "running"})

    if status == "error":
        # Surface the original HTTP status the synchronous endpoint used to
        # return, so the JS can branch on 503 ("llamafile down") the same
        # way it always has.
        http_status = int(snapshot.get("http_status") or 200)
        return jsonify({
            "status": "error",
            "error": snapshot.get("error") or "Unknown error",
            "http_status": http_status,
        }), 200  # always 200 — the JOB call succeeded; the WORK reports its own status

    # status == "done"
    return jsonify({
        "status": "done",
        "suggestions": snapshot.get("suggestions") or [],
        "transcript_preview": snapshot.get("transcript_preview") or "",
        "transcript_source": snapshot.get("transcript_source") or "",
    })
