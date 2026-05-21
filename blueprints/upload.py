"""Confirm + upload (SSE) + retry + thumbnail + results routes."""
from __future__ import annotations

import json
import os
import queue
import threading
import uuid

from flask import (
    Blueprint,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from core.config import load_config
from core.session_state import session
from core.upload_jobs import (
    drop_job,
    get_job,
    reap_stale_jobs,
    register_job,
    run_upload_job,
)

bp = Blueprint("upload", __name__)


def _safe_run_upload_job(job_id, *args, **kwargs):
    """M15: Outer wrapper around run_upload_job so an early crash (before its
    own try/finally takes over) still emits a terminal event and marks the
    job done — otherwise the SSE consumer would hang on heartbeats until the
    reaper sweeps."""
    import logging
    import time
    log = logging.getLogger(__name__)
    try:
        run_upload_job(job_id, *args, **kwargs)
    except BaseException as exc:
        log.exception("run_upload_job crashed for job %s", job_id)
        job = get_job(job_id)
        if job is not None:
            try:
                err = json.dumps({"type": "error", "message": f"Upload job crashed: {exc}"})
                job["queue"].put_nowait(err)
            except Exception:
                log.warning("could not enqueue terminal error event for job %s", job_id, exc_info=True)
            try:
                job["queue"].put_nowait('{"type": "done"}')
            except Exception:
                log.warning("could not enqueue terminal done event for job %s", job_id, exc_info=True)
            job["done"] = True
            job["finished_at"] = time.time()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise


@bp.route("/confirm")
def confirm():
    if not session.entries:
        flash("No dates selected. Please select dates first.", "warning")
        return redirect(url_for("scan.index"))

    summary = session.get_summary()
    max_workers = load_config().get("upload", {}).get("max_workers", 4)
    return render_template("confirm.html", summary=summary, max_workers=max_workers)


@bp.route("/upload", methods=["POST"])
def upload():
    """Start uploads in a background thread and return {job_id} immediately."""
    skip_list = request.form.getlist("skip")
    skip_set = set(skip_list)
    # Force a flush of any debounced writes before we snapshot — otherwise
    # the resume row and the upload thread can disagree about the user's
    # most recent edits.
    session.flush_pending_save()
    summary = session.get_summary()
    entries_snapshot = dict(session.entries)

    job_id = str(uuid.uuid4())
    reap_stale_jobs()
    register_job(job_id)
    current_session_id = session.session_id

    thread = threading.Thread(
        target=_safe_run_upload_job,
        args=(job_id, skip_set, summary, entries_snapshot, current_session_id),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@bp.route("/upload/stream")
def upload_stream():
    """SSE stream for a running upload job."""
    job_id = request.args.get("job_id", "")
    job = get_job(job_id)
    if job is None:
        def _not_found():
            yield 'data: {"type": "error", "message": "Job not found"}\n\n'
        return Response(_not_found(), mimetype="text/event-stream", status=404)

    def generate():
        try:
            while True:
                try:
                    msg = job["queue"].get(timeout=30)
                    yield f"data: {msg}\n\n"
                    parsed = json.loads(msg)
                    # Quota tracking lives inside the YouTube uploader now —
                    # it charges off the actual API response, not the SSE
                    # event, so failed uploads no longer bill quota.
                    if parsed.get("type") == "done":
                        break
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    if job["done"]:
                        break
                except Exception as exc:
                    # H7: any unexpected error in the SSE loop must terminate
                    # the stream cleanly instead of leaving the consumer
                    # hanging on its 30s heartbeat poll.
                    payload = json.dumps({"type": "error", "message": f"Stream error: {exc}"})
                    yield f"data: {payload}\n\n"
                    yield 'data: {"type": "done"}\n\n'
                    break
        finally:
            # Successful drain: reclaim immediately. If the client disconnected
            # mid-stream we leave the entry for reap_stale_jobs to clean up
            # on the next job creation.
            if job.get("done"):
                drop_job(job_id)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/upload/retry", methods=["POST"])
def upload_retry():
    """Re-queue a single failed upload item identified by row_id."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    row_id = data.get("row_id", "")
    summary = session.get_summary()
    matching_idx = None
    matching_item = None
    for i, item in enumerate(summary):
        if f"{item['date']}_{item['platform']}" == row_id:
            matching_idx = i
            matching_item = item
            break

    if matching_item is None:
        return jsonify({"error": "Item not found"}), 404

    entries_snapshot = dict(session.entries)
    job_id = str(uuid.uuid4())
    reap_stale_jobs()
    register_job(job_id)

    thread = threading.Thread(
        target=_safe_run_upload_job,
        args=(job_id, set(), [matching_item], entries_snapshot, session.session_id),
        kwargs={"row_offset": matching_idx, "is_retry": True},
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


@bp.route("/results")
def results():
    """Show upload results after completion."""
    all_results = []
    for date, platforms in session.upload_results.items():
        for platform, result in platforms.items():
            all_results.append({
                "date": date,
                "platform": platform,
                **result,
            })
    return render_template("confirm.html", summary=session.get_summary(), results=all_results)


@bp.route("/thumbnail")
def thumbnail():
    """Serve a thumbnail image for a given date."""
    iso_date = request.args.get("date", "")
    if not iso_date or iso_date not in session.entries:
        return "Not found", 404

    entry = session.entries[iso_date]
    thumb_path = entry.thumbnail_path
    if not thumb_path or not os.path.isfile(thumb_path):
        return "Not found", 404

    resp = send_file(thumb_path)
    resp.headers["Cache-Control"] = "max-age=3600"
    return resp
