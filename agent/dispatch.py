"""Agent control plane. Receives ``job_plan`` frames from the transport,
installs the credentials/db shims, resolves local file paths from the
cached scan, runs the orchestrator, and pumps every emitted event back
through the transport.

PR-C adds: EventBuffer (bounded replay on reconnect), PendingResults
(accumulate success rows for hello-frame replay).
"""
from __future__ import annotations

import logging
import threading as _thr
from collections import deque
from typing import Any

from agent import scan as _scan
from agent import secrets_shim as _sshim
from agent import db_shim as _dshim
from agent import run_batch as _rb

_logger = logging.getLogger(__name__)

# Indirection so tests can monkeypatch without touching run_batch directly.
_run_batch_run = _rb.run


# ---------------------------------------------------------------------------
# Cooperative cancel — one Event per active job_id
# ---------------------------------------------------------------------------
# The server sends ``{"type": "cancel_job", "job_id": "..."}`` over the
# control plane when the user clicks "Cancel" in the dashboard. The agent's
# main.py routes that frame to ``signal_cancel`` here; handle_job_plan
# registers a fresh Event keyed by job_id before launching run_batch and
# clears it on completion.
_cancel_events: dict[str, _thr.Event] = {}
_cancel_lock = _thr.RLock()


def _register_cancel(job_id: str) -> "_thr.Event":
    """Create and register a fresh cancel Event for *job_id*."""
    evt = _thr.Event()
    with _cancel_lock:
        _cancel_events[job_id] = evt
    return evt


def _unregister_cancel(job_id: str) -> None:
    """Drop the cancel Event for *job_id* (call in handle_job_plan's finally)."""
    with _cancel_lock:
        _cancel_events.pop(job_id, None)


def signal_cancel(job_id: str) -> bool:
    """Mark *job_id* as cancelled. Returns True if a matching job existed.

    Called by ``agent/main.py:_on_message`` when a ``cancel_job`` frame
    arrives. Best-effort: if the job_id is unknown (already finished or
    never started) we silently return False — the server forwards based
    on whichever device is online, not on whether the job is live.
    """
    with _cancel_lock:
        evt = _cancel_events.get(job_id)
    if evt is None:
        return False
    evt.set()
    return True


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
    job_id = plan.get("job_id") if isinstance(plan, dict) else None
    _rows = (plan.get("rows") or plan.get("payload", {}).get("rows") or []
             ) if isinstance(plan, dict) else []
    _logger.info(
        "dispatch: handling job_plan job=%s rows=%d",
        (job_id or "?")[:8], len(_rows),
    )

    def _emit(frame: dict) -> None:
        # Stamp job_id on every outgoing frame so the server can route it.
        if "job_id" not in frame:
            frame = {**frame, "job_id": job_id}
        # C2: record completed rows so the next hello can replay them.
        _pending_results.observe(frame)
        try:
            transport.send(frame)
        except Exception as exc:
            _logger.warning("transport.send failed: %s", exc)

    # TYPE-001: validate the envelope shape before doing any work. A malformed
    # or schema-drifted frame (not a dict, missing job_id, or rows not a list)
    # previously KeyError'd on plan["job_id"]/plan["rows"] BEFORE the try below,
    # was swallowed by agent/main.py with NO error frame emitted, and left the
    # dashboard's SSE stream hanging forever ("clicked Upload, agent did
    # nothing"). Emit a clean error + terminal done so the stream closes.
    if not isinstance(plan, dict) or not job_id or not isinstance(_rows, list):
        _emit({"type": "event", "event": "error",
               "error": "malformed job_plan: missing job_id or rows"})
        _emit({"type": "event", "event": "done"})
        return

    # Install shims fresh for this job — credentials from the envelope.
    shim = _sshim.install_as_core_secrets_store(
        initial=dict(plan.get("credentials") or {}),
        emit=_emit,
    )
    _dshim.install_as_core_db(emit=_emit)

    paths = _resolve_paths(_rows)
    cancel_evt = _register_cancel(job_id)
    try:
        _run_batch_run(envelope=plan, paths=paths, emit=_emit,
                       cancel_event=cancel_evt)
    except Exception as exc:
        _logger.exception("run_batch crashed: %s", exc)
        _emit({"type": "event", "event": "error",
               "error": f"run_batch crashed: {exc}"})
        _emit({"type": "event", "event": "done"})
    finally:
        _unregister_cancel(job_id)
        # Zeroize credentials at rest the moment the job is done so they
        # don't linger in process memory between jobs. The Fernet key
        # used to encrypt them is also dropped here — see
        # agent/secrets_shim.py docstring for the residency story and
        # its limitations.
        try:
            shim.shutdown()
        except Exception:
            _logger.debug("shim.shutdown() raised; suppressing", exc_info=True)


# ---------------------------------------------------------------------------
# C1 — Bounded event buffer with replay on reconnect
# ---------------------------------------------------------------------------

class EventBuffer:
    """Buffers emitted frames while disconnected; replays in order on reconnect.

    Thread-safe.  ``send`` is called under the lock so callers must ensure
    their send function is re-entrant-safe (or tolerant of being called from
    any thread).

    Args:
        max_size: Maximum number of frames to retain while disconnected.
                  When full, the *oldest* frame is dropped (ring-buffer style).
        send:     Callable invoked for each frame that should go to the wire.
    """

    def __init__(self, *, max_size: int, send) -> None:
        self._max = max_size
        self._send = send
        self._q: deque[dict] = deque()
        self._connected = False
        self._lock = _thr.RLock()

    def set_connected(self, connected: bool) -> None:
        """Mark the connection state.  On transition to *True*, flush buffer."""
        with self._lock:
            self._connected = connected
            if connected:
                while self._q:
                    self._send(self._q.popleft())

    def emit(self, frame: dict) -> None:
        """Send *frame* immediately if connected; otherwise buffer it."""
        with self._lock:
            if self._connected:
                self._send(frame)
                return
            if len(self._q) >= self._max:
                self._q.popleft()  # drop oldest
            self._q.append(frame)


# ---------------------------------------------------------------------------
# C2 — PendingResults: accumulate success rows for hello-frame replay
# ---------------------------------------------------------------------------

class PendingResults:
    """Records completed-row success events so they can be replayed in the
    hello frame after a reconnect.

    Keyed by ``(job_id, row_idx, platform)`` — last write wins (idempotent
    from the agent's perspective; server applies them idempotently too).
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, int, str], dict] = {}
        self._lock = _thr.RLock()

    def observe(self, frame: dict) -> None:
        """Record *frame* if it is a success event; ignore everything else.

        Uses ``.get()`` for every field: a success frame that's missing
        job_id/row_idx/platform (malformed, or emitted by a path that didn't
        stamp them) must be skipped, never KeyError out of the emit hot path
        and kill the job's event stream.
        """
        if frame.get("type") != "event" or frame.get("event") != "success":
            return
        job_id = frame.get("job_id")
        row_idx = frame.get("row_idx")
        platform = frame.get("platform")
        if job_id is None or row_idx is None or not platform:
            return
        key = (job_id, row_idx, platform)
        entry = {
            "job_id": job_id,
            "row_idx": row_idx,
            "iso_date": frame.get("iso_date", ""),
            "platform": platform,
            "status": "success",
            "payload": frame.get("payload", {}),
        }
        with self._lock:
            self._by_key[key] = entry

    def snapshot(self) -> list[dict]:
        """Return a copy of all recorded entries (order is insertion order)."""
        with self._lock:
            return list(self._by_key.values())

    def clear_acked(self, keys) -> None:
        """Remove entries whose keys the server has acknowledged.

        *keys* is an iterable of ``[job_id, row_idx, platform]`` triples
        (lists or tuples — both accepted).
        """
        with self._lock:
            for k in keys:
                self._by_key.pop(tuple(k), None)


# Module-level PendingResults singleton used by handle_job_plan so that
# every success event flowing through any job is observed.
_pending_results: PendingResults = PendingResults()
