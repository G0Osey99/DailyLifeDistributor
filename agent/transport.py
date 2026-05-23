"""Outbound wss client to the VPS relay. Sends a hello handshake, then a
receive loop that hands each decoded message to a callback. Reconnect with
backoff is the caller's concern via connect()/run_once().

PR-C: hello frame now carries ``pending_results`` when any completed-row
entries are buffered. Incoming ``pending_results_ack`` clears the acked
keys from the module-level PendingResults singleton in agent/dispatch.py.
"""
from __future__ import annotations

import json
import threading
from typing import Callable, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

import simple_websocket

PROTOCOL_VERSION = 1

# How long (seconds) each blocking receive waits before re-checking the
# shutdown event.  Small enough to feel responsive; large enough to be cheap.
_RECV_POLL_INTERVAL = 1.0


def _connect(url: str):
    """Seam for tests: real WebSocket client."""
    return simple_websocket.Client(url)


def _to_ws_url(server_url: str, token: str) -> str:
    parts = urlsplit(server_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, "/agent/socket",
                       urlencode({"token": token}), ""))


def _build_hello(pending_results: Optional[list] = None) -> dict:
    """Compose the hello frame, optionally including pending_results."""
    frame: dict = {"v": PROTOCOL_VERSION, "type": "hello",
                   "payload": {"role": "agent"}}
    if pending_results:
        frame["pending_results"] = pending_results
    return frame


class AgentConnection:
    def __init__(self, server_url: str, token: str,
                 shutdown_event: Optional[threading.Event] = None):
        self.server_url = server_url
        self.token = token
        self.ws = None
        # If provided, run_once will return False when this event is set so
        # the caller's message loop exits cleanly without blocking on recv().
        self._shutdown = shutdown_event or threading.Event()

    def connect(self, pending_results: Optional[list] = None) -> None:
        """Open the WebSocket and send the hello frame.

        *pending_results* is the snapshot from the module-level
        ``PendingResults`` instance in ``agent.dispatch``; when non-empty
        it is embedded in the hello so the server can apply it idempotently
        before the normal event stream resumes.
        """
        self.ws = _connect(_to_ws_url(self.server_url, self.token))
        self.ws.send(json.dumps(_build_hello(pending_results)))

    def send(self, message: dict) -> None:
        self.ws.send(json.dumps(message))

    def run_once(self, on_message: Callable[[dict], None]) -> bool:
        """Receive one message and dispatch it. Returns False when the
        connection has closed or the shutdown event has been set.

        Uses a short polling timeout (_RECV_POLL_INTERVAL) instead of
        ``timeout=None`` so the loop can notice a shutdown request even when
        the server is idle and no messages are arriving.

        Distinguishes poll timeout from disconnect:
          - ``receive(timeout=...)`` returning ``None`` is the poll-timeout
            signal (no data arrived within the window). We continue the
            inner loop so the shutdown event can fire and idle ticks
            don't cause spurious reconnects.
          - A real disconnect raises ``simple_websocket.ConnectionClosed``
            (or ``ConnectionError``); we catch those and return False so
            the outer loop in ``agent/main.run()`` reconnects.

        Handles ``pending_results_ack`` internally: clears the acked keys
        from ``agent.dispatch._pending_results`` before invoking on_message
        so callers don't need to know about the reconciliation protocol.
        """
        while not self._shutdown.is_set():
            try:
                raw = self.ws.receive(timeout=_RECV_POLL_INTERVAL)
            except (simple_websocket.ConnectionClosed,
                    simple_websocket.ConnectionError):
                # Real disconnect — caller will reconnect.
                return False
            if raw is None:
                # Poll timeout: loop and re-check shutdown event.
                continue
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("type") == "pending_results_ack":
                # C3: clear the acked keys from the module-level singleton.
                try:
                    from agent.dispatch import _pending_results
                    _pending_results.clear_acked(msg.get("acked") or [])
                except Exception:
                    pass  # non-fatal; worst case we re-send on next reconnect
            on_message(msg)
            return True
        # Shutdown event was set — signal the caller to exit the message loop.
        return False

    def close(self) -> None:
        if self.ws:
            self.ws.close()
