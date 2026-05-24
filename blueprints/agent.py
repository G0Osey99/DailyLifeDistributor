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
from typing import Deque, Dict, Tuple

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
    """Generate a single-use pairing code (session-gated by _require_auth).

    Multi-tenant phase β: the session user_id (if present) is recorded
    against the code so the agent's subsequent /agent/pair/redeem inherits
    it onto the new device row.
    """
    uid_raw = session.get("user_id")
    user_id: int | None
    try:
        user_id = int(uid_raw) if uid_raw is not None else None
    except (TypeError, ValueError):
        user_id = None
    code = devices.create_pairing_code(user_id=user_id)
    return jsonify({"code": code})


@bp.route("/agent/pair/redeem", methods=["POST"])
def pair_redeem():
    """Redeem a pairing code for a device token (no session — agent has none yet).

    Optional JSON fields ``hwid_hash`` (sha256 hex, ~64 chars) and
    ``hostname`` (friendly name, <=64 chars) are persisted on the device
    record so the dashboard can render a meaningful picker. Missing or
    empty values are stored as NULL — older agents that don't send the
    fields still pair successfully.
    """
    data = request.get_json(silent=True) or {}
    hwid_hash = (data.get("hwid_hash") or "").strip() or None
    hostname = (data.get("hostname") or "").strip() or None
    # Defensive caps — these are server-side trusts. hwid_hash is exactly
    # 64 hex chars in practice; hostname capped at 64 by the agent but
    # re-cap here so a tampered client can't bloat the row.
    if hwid_hash and len(hwid_hash) > 128:
        hwid_hash = hwid_hash[:128]
    if hostname and len(hostname) > 64:
        hostname = hostname[:64]
    # Re-link UX: if the agent reports an hwid_hash that already matches
    # an existing non-revoked device, the user almost certainly reinstalled
    # on the same hardware. Revoke the stale row + carry its friendly name
    # over to the new row so the dashboard's device picker doesn't fill up
    # with abandoned duplicates. The user still has to enter the pairing
    # code, so consent is unchanged.
    relinked = False
    inherited_name = None
    if hwid_hash:
        prior = devices.find_by_hwid(hwid_hash)
        if prior and not prior.get("revoked"):
            # Carry the old friendly name (the agent only knows its
            # system hostname; users typically renamed it post-pair).
            inherited_name = prior.get("name")
            devices.revoke_device(prior["id"])
            relinked = True

    name = (data.get("name") or "device").strip()
    if relinked and inherited_name:
        name = inherited_name

    result = devices.redeem_pairing_code(
        (data.get("code") or "").strip(),
        name,
        hwid_hash=hwid_hash,
        hostname=hostname,
    )
    if result is None:
        return jsonify({"error": "invalid or expired code"}), 400
    device_id, token = result
    body = {"device_id": device_id, "token": token}
    if relinked:
        body["relinked"] = True
        body["previous_name"] = inherited_name
        # Broadcast a `relinked` event to any currently-connected dashboard
        # browser sockets so the UI can toast "Re-linked agent <new-name>
        # (previously <old-name>)". Best-effort: if no browser is connected
        # or the broadcast layer isn't wired up yet (tests that bypass
        # register_sockets), the relink still completes — the toast is a
        # bonus signal, not a guarantee.
        try:
            RELAY.broadcast_to_browsers(_ACCOUNT, "relinked", {
                "device_id": device_id,
                "new_name": name,
                "previous_name": inherited_name,
            })
        except Exception:  # noqa: BLE001 — relink already happened; toast is best-effort
            _log.debug("relinked broadcast failed", exc_info=True)
    return jsonify(body)


@bp.route("/agent/devices", methods=["GET"])
def list_devices():
    return jsonify({"devices": devices.list_devices()})


@bp.route("/agent/devices/online", methods=["GET"])
def list_devices_online():
    """List currently-connected agents with same_network annotation.

    Session-auth-gated by the global ``_require_auth`` before_request hook
    (this endpoint is NOT in ``_PUBLIC_ENDPOINTS``).

    Returns ``{devices: [{id, name, hostname, hwid_hash_short, last_seen_at,
    same_network}]}`` — one entry per agent currently registered on the
    relay. ``same_network`` is True when the agent's stored connect_ip
    equals the browser's _client_ip(). ``hwid_hash_short`` is the first
    8 chars of the stored sha256 (full hash isn't useful to the UI and
    leaks identifying entropy unnecessarily).
    """
    online = RELAY.online_agents(_ACCOUNT)
    browser_ip = _client_ip()
    # Look up persisted name/hostname/hwid for each online agent. We don't
    # want to issue N queries in the worst case; one bulk listing is fine.
    db_devices = {d["id"]: d for d in devices.list_devices()}

    out: list[dict] = []
    for entry in online:
        did = entry["device_id"]
        db_row = db_devices.get(did, {})
        hwid = db_row.get("hwid_hash") or ""
        out.append({
            "id": did,
            "name": entry.get("device_name") or db_row.get("name") or "device",
            "hostname": db_row.get("hostname"),
            "hwid_hash_short": hwid[:8] if hwid else None,
            "last_seen_at": db_row.get("last_seen_at"),
            "same_network": bool(
                entry.get("connect_ip")
                and browser_ip
                and entry["connect_ip"] == browser_ip
                and browser_ip != "unknown"
            ),
        })
    return jsonify({"devices": out})


@bp.route("/agent/devices/<device_id>/revoke", methods=["POST"])
def revoke_device(device_id):
    devices.revoke_device(device_id)
    return jsonify({"ok": True})


@bp.route("/agent/devices/<device_id>/name", methods=["POST"])
def rename_device(device_id):
    """Update the user-friendly name for *device_id*.

    Session-auth-gated by the global ``_require_auth`` before_request hook
    (this endpoint is NOT in ``_PUBLIC_ENDPOINTS``). The agent-reported
    hostname is immutable; this only edits the ``name`` column rendered in
    the device picker / management UI.

    Body: ``{"name": "Studio Mac"}`` (1..64 chars after trimming).
    Returns ``{"ok": true, "device": {...row...}}`` on success,
    ``{"error": "..."}`` with 400 / 404 / 410 on validation / not-found /
    revoked.
    """
    data = request.get_json(silent=True) or {}
    raw = data.get("name", "")
    try:
        ok = devices.set_device_name(device_id, raw)
    except devices.DeviceNameEmpty:
        return jsonify({"error": "name must not be empty"}), 400
    except devices.DeviceNameTooLong:
        return jsonify({
            "error": f"name must be at most {devices.DEVICE_NAME_MAX_LEN} chars"
        }), 400
    if not ok:
        # Either missing or revoked. Disambiguate with a SELECT.
        rows = devices.list_devices()
        match = next((r for r in rows if r["id"] == device_id), None)
        if match is None:
            return jsonify({"error": "device not found"}), 404
        # Found but revoked.
        return jsonify({"error": "device is revoked"}), 410
    rows = devices.list_devices()
    updated = next((r for r in rows if r["id"] == device_id), None)
    return jsonify({"ok": True, "device": updated})


def _session_key() -> str:
    """Stable per-session identifier for session-keyed rate limits.

    Uses the signed session cookie identity (set at login). Falls back to
    the remote address so an unauthenticated request still gets a key.
    """
    return session.get("user_id") or session.get("_id") or request.remote_addr or "anon"


def _client_ip() -> str:
    """Return the *real* client IP for the current Flask request.

    Cloudflare strips client-supplied CF-Connecting-IP and sets it to the
    actual client; on the hosted deploy that's the only trustworthy value
    (request.remote_addr is the Caddy container). Locally we fall back
    through standard proxy headers and finally request.remote_addr.

    Used in two places:
      1. Recording the agent's connect IP at WebSocket handshake.
      2. Computing same_network in /agent/devices/online.

    Both compare strings; the only constraint is consistency between
    "what the agent sees" and "what the browser sees" — Cloudflare
    routes both through the same CF-Connecting-IP normalization, so a
    browser + agent on the same LAN match on the egress IP.
    """
    cf = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf:
        return cf
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        # First entry is the original client; the rest are proxies.
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    return request.remote_addr or "unknown"


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
                    # WS already half-closed — peer never sees the
                    # error frame. Non-fatal but worth a trace for triage.
                    _log.debug(
                        "agent_socket: rate-limit notice send failed for ip=%s",
                        ip, exc_info=True,
                    )
                return

        token = request.args.get("token", "")
        device_id = verify_device_token(token)
        if not device_id:
            ws.send(_json.dumps({"v": 1, "type": "error",
                                 "payload": {"reason": "unauthorized"}}))
            return
        touch_device(device_id)
        _device_name = devices.get_device_name(device_id)
        # Capture the client IP at handshake so the dashboard can later
        # compute same_network for the device picker. _client_ip() honors
        # CF-Connecting-IP behind Cloudflare and X-Forwarded-For elsewhere.
        _connect_ip = _client_ip()
        RELAY.register_agent(_ACCOUNT, device_id, ws.send,
                             device_name=_device_name,
                             connect_ip=_connect_ip)

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
                        _log.debug(
                            "agent_socket: rate-limit notice send failed "
                            "for device=%s",
                            device_id[:8] if device_id else "?",
                            exc_info=True,
                        )
                    break
                # Route frames that target the server (event,
                # credentials_updated, image_used). on_frame logs unrecognised
                # types at debug and drops them.
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
                                   "image_used"):
                        _agent_dispatch.on_frame(frame)
                        continue
                except Exception:
                    # A malformed frame (bad JSON, unexpected shape, or a
                    # transient bug in on_frame) must not bring the socket
                    # down — we fall through to route_from_agent so the
                    # browser can still see legitimate broadcasts. Log at
                    # debug with exc_info so triage has a stack on the
                    # rare occasions an operator pulls logs.
                    _log.debug(
                        "agent_socket frame dispatch raised for device=%s",
                        device_id[:8] if device_id else "?",
                        exc_info=True,
                    )
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
