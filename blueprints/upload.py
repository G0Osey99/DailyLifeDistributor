"""Upload progress SSE stream.

The browser-streaming pipeline (blueprints/media.py) drives uploads per batch
and pushes per-row events onto a per-job queue; this blueprint exposes the
shared `/upload/stream` SSE endpoint the dashboard consumes. The legacy
server-side `/upload`, `/confirm`, `/review`, `/results`, and `/thumbnail`
routes were removed when the dashboard replaced the old three-page flow.
"""
from __future__ import annotations

import json
import queue

from flask import Blueprint, Response, request

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
