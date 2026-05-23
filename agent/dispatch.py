"""Agent control plane. Receives ``job_plan`` frames from the transport,
installs the credentials/db shims, resolves local file paths from the
cached scan, runs the orchestrator, and pumps every emitted event back
through the transport.

Phase 3 keeps the pending_results / buffer logic deferred to PR-C.
"""
from __future__ import annotations

import logging
from typing import Any

from agent import scan as _scan
from agent import secrets_shim as _sshim
from agent import db_shim as _dshim
from agent import run_batch as _rb

_logger = logging.getLogger(__name__)

# Indirection so tests can monkeypatch without touching run_batch directly.
_run_batch_run = _rb.run


def _resolve_paths(rows: list[dict]) -> dict[str, dict[str, str]]:
    """For each row's iso_date return {kind: local_path} from the scan cache.

    Falls back to a fresh scan() call if latest_results() is empty (i.e. the
    agent hasn't received a scan_request yet in this session).  This keeps
    the dispatch self-contained for unit tests that don't prime the cache.
    """
    cached = _scan.latest_results()
    if not cached:
        # No cached scan — perform a fresh one against the configured roots.
        cached = _scan.scan()
    return {row["iso_date"]: cached.get(row["iso_date"], {}) for row in rows}


def handle_job_plan(*, plan: dict, transport: Any) -> None:
    """Execute a ``job_plan`` envelope end-to-end.

    1. Install fresh secrets + db shims (credentials come from the envelope).
    2. Resolve local file paths from the scan cache.
    3. Run the orchestrator, pumping every event frame through the transport.

    This function blocks until the job completes (or crashes).  It is
    intended to be called from a background thread in agent/main.py.
    """
    job_id = plan["job_id"]

    def _emit(frame: dict) -> None:
        # Stamp job_id on every outgoing frame so the server can route it.
        if "job_id" not in frame:
            frame = {**frame, "job_id": job_id}
        try:
            transport.send(frame)
        except Exception as exc:
            _logger.warning("transport.send failed: %s", exc)

    # Install shims fresh for this job — credentials from the envelope.
    _sshim.install_as_core_secrets_store(
        initial=dict(plan.get("credentials") or {}),
        emit=_emit,
    )
    _dshim.install_as_core_db(emit=_emit)

    paths = _resolve_paths(plan["rows"])
    try:
        _run_batch_run(envelope=plan, paths=paths, emit=_emit)
    except Exception as exc:
        _logger.exception("run_batch crashed: %s", exc)
        _emit({"type": "event", "event": "error",
               "error": f"run_batch crashed: {exc}"})
        _emit({"type": "event", "event": "done"})
