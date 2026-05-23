"""Hybrid upload agent: device pairing HTTP routes + WebSocket relay.

Phase 1 — pairing/token endpoints and the relay sockets.
Phase 3 hardening — rate-limit pair + agent routes (HTTP), throttle
per-WebSocket message rate, cap concurrent ws connects per IP/session.
The blueprint and sockets are only registered when HYBRID_AGENT_ENABLED.
"""
from __future__ import annotations

import json as _json
import logging
import os
import secrets
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional, Tuple

from flask import Blueprint, abort, jsonify, request, send_file, session

from blueprints.auth import is_authenticated as _is_authenticated
from core import devices
from core import relay as _relay_mod
from core import agent_dispatch as _agent_dispatch
from core.devices import touch_device, verify_device_token

bp = Blueprint("agent", __name__)
_log = logging.getLogger(__name__)

# Largest control message the relay will forward. Phase 1 only carries tiny
# JSON envelopes (ping/pong/hello/presence); the cap is defense-in-depth so a
# misbehaving peer can't buffer/forward an oversized message.
_MAX_MESSAGE_BYTES = 65_536


# ---------------------------------------------------------------------------
# Per-WebSocket message rate limiter (token bucket)
# ---------------------------------------------------------------------------
# A paired-but-malicious or buggy agent could flood the relay with junk
# messages. The token bucket caps each connection at WS_MSG_BUDGET messages
# per WS_MSG_WINDOW seconds. Excess closes the socket cleanly with a
# rate_limit_exceeded reason — the peer must reconnect.
WS_MSG_BUDGET = 100
WS_MSG_WINDOW = 10.0  # seconds


class _TokenBucket:
    """Sliding-window message rate limiter.

    Holds a deque of recent message timestamps. ``allow()`` returns False
    once WS_MSG_BUDGET timestamps inside the last WS_MSG_WINDOW seconds
    have accumulated. Thread-safe (per-connection — each socket gets its
    own bucket, but ws.receive() in flask-sock can be called from worker
    threads so locking is cheap insurance).
    """

    __slots__ = ("_q", "_budget", "_window", "_lock")

    def __init__(self, budget: int = WS_MSG_BUDGET, window: float = WS_MSG_WINDOW):
        self._q: Deque[float] = deque()
        self._budget = budget
        self._window = window
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            # Trim out-of-window stamps from the front.
            while self._q and self._q[0] < cutoff:
                self._q.popleft()
            if len(self._q) >= self._budget:
                return False
            self._q.append(now)
            return True


# ---------------------------------------------------------------------------
# WebSocket connect rate limiter (fixed window per key)
# ---------------------------------------------------------------------------
# flask-sock routes don't participate in flask-limiter's request-time hooks,
# so we cap connect attempts manually. The counters are per-IP for /agent/socket
# and per-session for /agent/ws. Fixed 60s windows; in-memory only.
_CONN_LIMITS: Dict[str, "_FixedWindowCounter"] = {
    "agent_socket": None,  # type: ignore[assignment]
    "agent_ws": None,  # type: ignore[assignment]
}


class _FixedWindowCounter:
    """One-minute fixed-window counter keyed by client identifier."""

    __slots__ = ("_budget", "_window", "_counts", "_lock")

    def __init__(self, budget: int, window: float = 60.0):
        self._budget = budget
        self._window = window
        # key -> (window_start_monotonic, count)
        self._counts: Dict[str, Tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            window_start, count = self._counts.get(key, (now, 0))
            if now - window_start >= self._window:
                # New window.
                window_start, count = now, 0
            count += 1
            self._counts[key] = (window_start, count)
            return count <= self._budget


def _rl_enabled() -> bool:
    """Check the Flask app's RATELIMIT_ENABLED flag at call time.

    flask-limiter exposes a similar global flag; we mirror it for the manual
    ws-connect counters so the test suite (TESTING + RATELIMIT_ENABLED=False)
    isn't false-429ed.
    """
    from flask import current_app
    try:
        return bool(current_app.config.get("RATELIMIT_ENABLED", True))
    except RuntimeError:
        return True


@bp.route("/agent/pair/new", methods=["POST"])
def pair_new():
    """Generate a single-use pairing code (session-gated by _require_auth)."""
    code = devices.create_pairing_code()
    return jsonify({"code": code})


@bp.route("/agent/pair/redeem", methods=["POST"])
def pair_redeem():
    """Redeem a pairing code for a device token (no session — agent has none yet)."""
    data = request.get_json(silent=True) or {}
    result = devices.redeem_pairing_code(
        (data.get("code") or "").strip(), (data.get("name") or "device").strip())
    if result is None:
        return jsonify({"error": "invalid or expired code"}), 400
    device_id, token = result
    return jsonify({"device_id": device_id, "token": token})


@bp.route("/agent/devices", methods=["GET"])
def list_devices():
    return jsonify({"devices": devices.list_devices()})


@bp.route("/agent/devices/<device_id>/revoke", methods=["POST"])
def revoke_device(device_id):
    devices.revoke_device(device_id)
    return jsonify({"ok": True})


def _session_key() -> str:
    """Stable per-session identifier for session-keyed rate limits.

    Uses the signed session cookie identity (set at login). Falls back to
    the remote address so an unauthenticated request still gets a key.
    """
    return session.get("user_id") or session.get("_id") or request.remote_addr or "anon"


def attach_limits(app, limiter) -> None:
    """Apply rate limits to pair_new + pair_redeem after blueprint registration.

    flask-limiter's ``limiter.limit(...)`` decorator stashes the limit on
    the wrapped view function. Because we register the agent blueprint at
    app-creation time but the limiter is only built once the Flask app
    exists, we apply the decorators here against ``app.view_functions``,
    which is the *only* dict Flask actually dispatches against — replacing
    the bound view in this dict takes effect for every future request.

    Called once at app startup after the agent blueprint is registered.
    The decorator-shaped split avoids a circular import (the limiter lives
    on the app object, which doesn't exist yet when this module loads).

    Limits:
      * pair_new     — 10 per hour per session (authenticated)
      * pair_redeem  — 5 per minute per IP (unauthenticated, brute-force target)
    """
    for endpoint, decorator in (
        ("agent.pair_new", limiter.limit("10 per hour", key_func=_session_key)),
        ("agent.pair_redeem", limiter.limit("5 per minute")),  # default key=IP
    ):
        view = app.view_functions.get(endpoint)
        if view is None:
            _log.debug("attach_limits: endpoint %s not found; skipping", endpoint)
            continue
        app.view_functions[endpoint] = decorator(view)


# ---------------------------------------------------------------------------
# WebSocket relay handlers (registered via register_sockets, not as blueprint
# routes, because flask-sock uses a Sock instance bound to the app directly).
# ---------------------------------------------------------------------------
# One process-wide relay shared by all sockets.
RELAY = _relay_mod.Relay()

# Single shared account for now (shared-password deploy). Future multi-tenant
# work keys rooms by real account id.
_ACCOUNT = "default"


def register_sockets(sock) -> None:
    """Register the agent + browser WebSocket routes on a flask_sock.Sock."""
    # Wire the process-wide relay so agent_dispatch.send_to_device works
    # without importing blueprints (which would create a circular dependency).
    _relay_mod.set_default_relay(RELAY, account=_ACCOUNT)

    # Lazily build the connect counters (test suite resets these on TESTING).
    _CONN_LIMITS["agent_socket"] = _FixedWindowCounter(budget=20, window=60.0)
    _CONN_LIMITS["agent_ws"] = _FixedWindowCounter(budget=60, window=60.0)

    @sock.route("/agent/socket")
    def agent_socket(ws):
        # Per-IP connect cap (reconnect storms / scanning).
        if _rl_enabled():
            ip = request.remote_addr or "anon"
            if not _CONN_LIMITS["agent_socket"].allow(ip):
                _log.warning("agent_socket: rate limit exceeded for %s", ip)
                try:
                    ws.send(_json.dumps({
                        "v": 1, "type": "error",
                        "payload": {"reason": "rate_limit_exceeded"},
                    }))
                except Exception:
                    pass
                return

        token = request.args.get("token", "")
        device_id = verify_device_token(token)
        if not device_id:
            ws.send(_json.dumps({"v": 1, "type": "error",
                                 "payload": {"reason": "unauthorized"}}))
            return
        touch_device(device_id)
        _device_name = devices.get_device_name(device_id)
        RELAY.register_agent(_ACCOUNT, device_id, ws.send, device_name=_device_name)

        # Per-connection message bucket — paired-but-malicious agents
        # can't flood the relay.
        bucket = _TokenBucket()

        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                if len(msg) > _MAX_MESSAGE_BYTES:
                    break
                if _rl_enabled() and not bucket.allow():
                    _log.warning(
                        "agent_socket: per-connection message rate limit "
                        "exceeded for device %s; closing", device_id,
                    )
                    try:
                        ws.send(_json.dumps({
                            "v": 1, "type": "error",
                            "payload": {"reason": "rate_limit_exceeded"},
                        }))
                    except Exception:
                        pass
                    break
                # Route frames that target the server (event, and future
                # credentials_updated / image_used / pending_results_chunk).
                # on_frame handles unrecognised types as a debug no-op so
                # adding new types in A7/A8/A9 is a small switch addition.
                try:
                    frame = _json.loads(msg)
                    ftype = frame.get("type") if isinstance(frame, dict) else None
                    if ftype == "hello":
                        # C3: apply any pending_results the agent buffered
                        # while disconnected, then ack so it can clear them.
                        pending = frame.get("pending_results") or []
                        if pending:
                            try:
                                acked = _agent_dispatch.apply_pending_results(pending)
                                ws.send(_json.dumps({
                                    "v": 1,
                                    "type": "pending_results_ack",
                                    "acked": [list(k) for k in acked],
                                }))
                            except Exception as _exc:
                                _log.warning(
                                    "apply_pending_results failed: %s", _exc)
                        # Don't continue — hello also needs to reach the relay
                        # for presence tracking (fall through to route_from_agent).
                    elif ftype in ("event", "credentials_updated",
                                   "image_used", "pending_results_chunk"):
                        _agent_dispatch.on_frame(frame)
                        continue
                except Exception:
                    pass
                RELAY.route_from_agent(_ACCOUNT, msg)
        finally:
            RELAY.unregister_agent(_ACCOUNT, device_id)

    @sock.route("/agent/ws")
    def agent_browser_socket(ws):
        # Session-authenticated. The global _require_auth lets the upgrade GET
        # through only for logged-in browsers; we re-check here via the shared
        # is_authenticated() so the auth key can never drift out of sync.
        if not _is_authenticated():
            ws.send(_json.dumps({"v": 1, "type": "error",
                                 "payload": {"reason": "unauthorized"}}))
            return

        # Per-session connect cap. The browser shouldn't reconnect more than
        # once a second on average; 60/min is generous.
        if _rl_enabled():
            sess_key = _session_key()
            if not _CONN_LIMITS["agent_ws"].allow(sess_key):
                _log.warning("agent_ws: rate limit exceeded for session %s",
                             sess_key)
                try:
                    ws.send(_json.dumps({
                        "v": 1, "type": "error",
                        "payload": {"reason": "rate_limit_exceeded"},
                    }))
                except Exception:
                    pass
                return

        session_id = secrets.token_hex(8)  # unique, unambiguous per connection
        RELAY.register_browser(_ACCOUNT, session_id, ws.send)
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                if len(msg) > _MAX_MESSAGE_BYTES:
                    break
                RELAY.route_from_browser(_ACCOUNT, msg)
        finally:
            RELAY.unregister_browser(_ACCOUNT, session_id)


from core import release_store as _release_store


@bp.route("/agent/releases/manifest.json", methods=["GET"])
def release_manifest():
    p = _release_store.manifest_path()
    if not os.path.isfile(p):
        abort(404)
    return send_file(p, mimetype="application/json")


@bp.route("/agent/releases/<filename>", methods=["GET"])
def release_binary(filename):
    p = _release_store.binary_path(filename)
    if p is None:
        abort(400)
    if not os.path.isfile(p):
        abort(404)
    return send_file(p, mimetype="application/octet-stream", as_attachment=True,
                     download_name=filename)
