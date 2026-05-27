"""Outbound wss client to the VPS relay. Sends a hello handshake, then a
receive loop that hands each decoded message to a callback. Reconnect with
backoff is the caller's concern via connect()/run_once().

PR-C: hello frame now carries ``pending_results`` when any completed-row
entries are buffered. Incoming ``pending_results_ack`` clears the acked
keys from the module-level PendingResults singleton in agent/dispatch.py.
"""
from __future__ import annotations

import json
import logging
import socket
import ssl
import threading
from typing import Callable, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

import certifi
import simple_websocket

PROTOCOL_VERSION = 1

# How long (seconds) each blocking receive waits before re-checking the
# shutdown event.  Small enough to feel responsive; large enough to be cheap.
_RECV_POLL_INTERVAL = 1.0

# Connect timeout for ``socket.create_connection`` + TLS handshake. Set
# via ``socket.setdefaulttimeout()`` around the Client() call because
# simple-websocket doesn't expose a connect_timeout kwarg on Client. If
# the WebSocket connect hangs (DNS resolves but TCP/TLS stalls — e.g.
# Cloudflare edge being weird, or a captive-portal DNS poisoning the
# response), we'd otherwise sit in the call forever and the GUI would
# show "Connecting…" with no timeout, no log, no recourse.
_CONNECT_TIMEOUT_S = 20.0

# App-level WebSocket keepalive cadence. We DO NOT pass ping_interval
# to ``simple_websocket.Client`` — the library's background ping thread
# calls ``self.ws.send(Ping())`` + ``self.sock.send(...)`` concurrently
# with user-thread sends, and neither wsproto's connection state nor
# the SSL socket is thread-safe. The field report
# (``[SSL] internal error (_ssl.c:2427)`` / ``EOF occurred in violation
# of protocol``) was the classic state-corruption signature.
#
# Instead the receive-loop thread sends a tiny app-level keepalive
# frame on this cadence, serialized through ``AgentConnection._send_lock``
# (which all outbound sends — worker-thread events, hello, keepalive —
# share). 15s comfortably beats the ~24s consumer-router NAT-drop
# window and stays well under Cloudflare's 100s WS idle cap.
_PING_INTERVAL_S = 15.0


def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSLContext that trusts the same CAs ``requests`` does.

    PyInstaller bundles certifi as a transitive dep of ``requests``, so
    the cert bundle ships inside the .app/.exe and works regardless of
    where the binary ends up on disk. Using
    ``ssl.create_default_context()`` without a ``cafile`` argument falls
    back to whatever Python was built against — on a python.org macOS
    installer that's certifi too, but on a PyInstaller .app bundle the
    trust-store lookup is fragile (the post-install
    ``Install Certificates.command`` script never ran, so OpenSSL's
    default cert path may point at a non-existent file). Result:
    ``ssl.create_default_context()`` returns a context with no trust
    anchors, every wss:// handshake fails verification, and the
    reconnect loop hangs forever with no visible error.
    """
    return ssl.create_default_context(cafile=certifi.where())


def _connect(url: str):
    """Seam for tests: real WebSocket client.

    Forces the SSL context to use the bundled certifi CA file (see
    ``_build_ssl_context``) so wss:// works in a PyInstaller .app, and
    bounds the connect+handshake at ``_CONNECT_TIMEOUT_S`` so a stalled
    handshake surfaces as ``OSError`` instead of hanging forever.

    Note: we deliberately do NOT pass ``ping_interval`` here. The
    library's ping thread races user-thread sends (see the
    ``_PING_INTERVAL_S`` doc above). ``AgentConnection`` runs its own
    single-threaded app-level keepalive instead.
    """
    parts = urlsplit(url)
    ssl_context: Optional[ssl.SSLContext] = (
        _build_ssl_context() if parts.scheme == "wss" else None
    )
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_CONNECT_TIMEOUT_S)
    try:
        return simple_websocket.Client(url, ssl_context=ssl_context)
    finally:
        socket.setdefaulttimeout(prev_timeout)


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
        # All outbound sends serialize through this lock. ``simple_websocket``
        # is not safe for concurrent writes — wsproto mutates connection
        # state in ``ws.send()`` and the SSL socket isn't reentrant either.
        # Concurrent writes manifested in the field as
        # ``[SSL] internal error (_ssl.c:2427)`` / ``EOF occurred in
        # violation of protocol`` (state corruption) and dropped the
        # WebSocket mid-job. The lock covers worker-thread event emission,
        # hello, and the keepalive in ``run_once``.
        self._send_lock = threading.Lock()
        # Tracks when we last put any frame on the wire so the keepalive
        # only sends when truly idle (the timer doesn't fire while uploads
        # are emitting progress events).
        self._last_send_at = 0.0

    def connect(self, pending_results: Optional[list] = None) -> None:
        """Open the WebSocket and send the hello frame.

        *pending_results* is the snapshot from the module-level
        ``PendingResults`` instance in ``agent.dispatch``; when non-empty
        it is embedded in the hello so the server can apply it idempotently
        before the normal event stream resumes.
        """
        import time as _time
        self.ws = _connect(_to_ws_url(self.server_url, self.token))
        with self._send_lock:
            self.ws.send(json.dumps(_build_hello(pending_results)))
            self._last_send_at = _time.monotonic()

    def send(self, message: dict) -> None:
        import time as _time
        with self._send_lock:
            self.ws.send(json.dumps(message))
            self._last_send_at = _time.monotonic()

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
        import time as _time
        while not self._shutdown.is_set():
            try:
                raw = self.ws.receive(timeout=_RECV_POLL_INTERVAL)
            except (simple_websocket.ConnectionClosed,
                    simple_websocket.ConnectionError):
                # Real disconnect — caller will reconnect.
                return False
            # App-level keepalive — emit a tiny frame whenever the last
            # send (any send, including upload events) was longer ago than
            # _PING_INTERVAL_S. Runs on this single receive thread so it
            # serializes naturally with worker-thread sends through
            # ``_send_lock``. Avoids the simple_websocket ping-thread race
            # that produced ``[SSL] internal error`` in the field.
            if (_time.monotonic() - self._last_send_at) >= _PING_INTERVAL_S:
                try:
                    self.send({"v": PROTOCOL_VERSION, "type": "keepalive"})
                except (simple_websocket.ConnectionClosed,
                        simple_websocket.ConnectionError, OSError):
                    return False
            if raw is None:
                # Poll timeout: loop and re-check shutdown event.
                continue
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError, TypeError):
                # Malformed frame from the server — DON'T crash the
                # receive loop (the old behavior bubbled out of run_once
                # and looked like a disconnect). Skip the bad frame and
                # keep listening.
                _log = logging.getLogger(__name__)
                _log.warning(
                    "agent transport: dropping malformed WS frame "
                    "(%d bytes)", len(raw) if isinstance(raw, (str, bytes)) else -1,
                )
                continue
            if isinstance(msg, dict) and msg.get("type") == "pending_results_ack":
                # C3: clear the acked keys from the module-level singleton.
                try:
                    from agent.dispatch import _pending_results
                    _pending_results.clear_acked(msg.get("acked") or [])
                except Exception:
                    # Non-fatal: worst case we re-send on next reconnect.
                    logging.getLogger(__name__).debug(
                        "pending_results clear_acked failed", exc_info=True,
                    )
            on_message(msg)
            return True
        # Shutdown event was set — signal the caller to exit the message loop.
        return False

    def close(self) -> None:
        if not self.ws:
            return
        try:
            self.ws.close()
        except Exception:
            # simple_websocket raises ConnectionClosed (and friends) when the
            # peer has already sent a close frame — that's the normal shutdown
            # path, not an error. Anything else here is also non-actionable;
            # the socket is going away regardless.
            pass
        finally:
            self.ws = None
