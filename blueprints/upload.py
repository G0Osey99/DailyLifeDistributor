"""Upload progress SSE stream.

The browser-streaming pipeline (blueprints/media.py) drives uploads per batch
and pushes per-row events onto a per-job queue; this blueprint exposes the
shared `/upload/stream` SSE endpoint the dashboard consumes. The legacy
server-side `/upload`, `/confirm`, `/review`, `/results`, and `/thumbnail`
routes were removed when the dashboard replaced the old three-page flow.

Also exposes ``POST /upload/<job_id>/cancel`` — works for both upload paths:
* web path: sets a ``threading.Event`` the in-process run_batch worker polls
  before each row submission (and at the top of _upload_one).
* agent path: forwards a ``cancel_job`` frame to the owning agent over the
  relay (existing ``agent_dispatch.cancel_job``).
"""
from __future__ import annotations

import json
import queue

from flask import Blueprint, Response, jsonify, request

from core import upload_jobs
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
    """Cancel a running upload job.

    Two paths share this route:

    * **Web path** — the job's ``run_batch`` thread lives in this process.
      ``upload_jobs.signal_cancel(job_id)`` sets a ``threading.Event`` the
      worker polls before each row dispatch; pending rows short-circuit
      with ``error_type: cancelled``. In-flight rows (a YouTube chunk
      mid-upload, a Chrome page navigating) finish normally — cancellation
      is best-effort cooperative, not a hard kill.
    * **Agent path** — the job runs on a remote agent over the relay.
      ``agent_dispatch.cancel_job(job_id)`` forwards a ``cancel_job`` frame
      so the agent's local dispatcher sets its own cancel Event.

    We try the web path first because that's the in-process registry; if
    the job isn't registered there, we fall through to the agent dispatch.
    Returns:
      200 ``{ok: true}`` when cancel was signalled (web) or the frame was
        delivered (agent).
      404 ``{error: "job not found"}`` when neither registry knows the id.
      409 ``{error: "agent offline"}`` when the target agent isn't currently
        connected (agent path only).
    """
    # Web-path: check the upload_jobs registry first. The cancel Event is
    # created in register_job and removed in drop_job, so an unknown id
    # means the job either never started, already finished + was reaped,
    # or is an agent-only job (those don't get an in-process Event).
    if upload_jobs.signal_cancel(job_id):
        return jsonify({"ok": True})

    # Agent path fallback. HYBRID-disabled deploys won't have the module.
    try:
        from core import agent_dispatch
    except Exception:  # noqa: BLE001 — HYBRID disabled, no module
        return jsonify({"error": "job not found"}), 404
    try:
        agent_dispatch.cancel_job(job_id)
    except agent_dispatch.JobNotFoundError:
        return jsonify({"error": "job not found"}), 404
    except agent_dispatch.AgentOfflineError as exc:
        return jsonify({"error": "agent offline", "detail": str(exc)}), 409
    return jsonify({"ok": True})
