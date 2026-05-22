"""Hybrid upload agent: device pairing HTTP routes + WebSocket relay.

Phase 1 — pairing/token endpoints and the relay sockets. No uploads yet.
The blueprint and sockets are only registered when HYBRID_AGENT_ENABLED.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core import devices

bp = Blueprint("agent", __name__)


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


# ---------------------------------------------------------------------------
# WebSocket relay handlers (registered via register_sockets, not as blueprint
# routes, because flask-sock uses a Sock instance bound to the app directly).
# ---------------------------------------------------------------------------
import json as _json  # noqa: E402

from flask import session as _session  # noqa: E402

from core import relay as _relay_mod  # noqa: E402
from core.devices import verify_device_token, touch_device  # noqa: E402

# One process-wide relay shared by all sockets.
RELAY = _relay_mod.Relay()

# Single shared account for now (shared-password deploy). Future multi-tenant
# work keys rooms by real account id.
_ACCOUNT = "default"
_AUTH_SESSION_KEY = "authenticated"


def register_sockets(sock) -> None:
    """Register the agent + browser WebSocket routes on a flask_sock.Sock."""

    @sock.route("/agent/socket")
    def agent_socket(ws):
        token = request.args.get("token", "")
        device_id = verify_device_token(token)
        if not device_id:
            ws.send(_json.dumps({"v": 1, "type": "error",
                                 "payload": {"reason": "unauthorized"}}))
            return
        touch_device(device_id)
        RELAY.register_agent(_ACCOUNT, device_id, ws.send)
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                RELAY.route_from_agent(_ACCOUNT, msg)
        finally:
            RELAY.unregister_agent(_ACCOUNT, device_id)

    @sock.route("/agent/ws")
    def agent_browser_socket(ws):
        # Session-authenticated (the global _require_auth lets the upgrade GET
        # through only for logged-in browsers; double-check here too).
        if not _session.get(_AUTH_SESSION_KEY):
            ws.send(_json.dumps({"v": 1, "type": "error",
                                 "payload": {"reason": "unauthorized"}}))
            return
        session_id = _json.dumps(id(ws))  # unique per connection
        RELAY.register_browser(_ACCOUNT, session_id, ws.send)
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                RELAY.route_from_browser(_ACCOUNT, msg)
        finally:
            RELAY.unregister_browser(_ACCOUNT, session_id)
