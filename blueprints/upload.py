"""Upload progress SSE stream.

The browser-streaming pipeline (blueprints/media.py) drives uploads per batch
and pushes per-row events onto a per-job queue; this blueprint exposes the
shared `/upload/stream` SSE endpoint the dashboard consumes. The legacy
server-side `/upload`, `/confirm`, `/review`, `/results`, and `/thumbnail`
routes were removed when the dashboard replaced the old three-page flow.

Also exposes ``POST /upload/<job_id>/cancel`` — agent-path jobs only.
Web-only-path cancellation is a future addition (the run_batch thread
lives in-process and isn't reachable via the relay).
"""
from __future__ import annotations

import json
import queue

from flask import Blueprint, Response, jsonify, request

from core.upload_jobs import drop_job, get_job

bp = Blueprint("upload", __name__)


@bp.route("/upload/stream")
def upload_stream():
    """SSE stream for a running upload job (one batch of the media pipeline)."""
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
                    if parsed.get("type") == "done":
                        break
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    if job["done"]:
                        break
                except Exception as exc:  # noqa: BLE001
                    # Any unexpected error must terminate the stream cleanly
                    # instead of leaving the consumer hanging on heartbeats.
                    payload = json.dumps({"type": "error", "message": f"Stream error: {exc}"})
                    yield f"data: {payload}\n\n"
                    yield 'data: {"type": "done"}\n\n'
                    break
        finally:
            # Successful drain: reclaim immediately. A mid-stream client
            # disconnect leaves the entry for the reaper to sweep.
            if job.get("done"):
                drop_job(job_id)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/upload/<job_id>/cancel", methods=["POST"])
def upload_cancel(job_id):
    """Cancel a running agent-path job.

    Looks up the job in ``core.agent_dispatch`` and forwards a
    ``cancel_job`` frame to the owning agent over the relay. In-flight
    uploads on the agent complete normally; pending rows short-circuit
    with an ``error_type: cancelled`` event.

    Returns:
      200 ``{ok: true}`` when the cancel frame is delivered.
      404 ``{error: "job not found"}`` when the id isn't in the registry
        (already done, never started, or this is a web-only-path job).
      409 ``{error: "agent offline"}`` when the target device isn't
        currently connected to the relay.

    Web-only-path cancel is a TODO — that job's run_batch thread lives in
    ``core.upload_jobs`` and there's no relay frame to send.
    """
    try:
        from core import agent_dispatch
    except Exception:  # noqa: BLE001 — HYBRID disabled, no module
        return jsonify({"error": "agent path not enabled"}), 404
    try:
        agent_dispatch.cancel_job(job_id)
    except agent_dispatch.JobNotFoundError:
        return jsonify({"error": "job not found"}), 404
    except agent_dispatch.AgentOfflineError as exc:
        return jsonify({"error": "agent offline", "detail": str(exc)}), 409
    return jsonify({"ok": True})
