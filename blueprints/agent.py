"""Hybrid upload agent: device pairing HTTP routes + WebSocket relay.

Phase 1 — pairing/token endpoints and the relay sockets. No uploads yet.
The blueprint and sockets are only registered when HYBRID_AGENT_ENABLED.
"""
from __future__ import annotations

import json as _json
import os
import secrets

from flask import Blueprint, abort, jsonify, request, send_file

from blueprints.auth import is_authenticated as _is_authenticated
from core import devices
from core import relay as _relay_mod
from core import agent_dispatch as _agent_dispatch
from core.devices import touch_device, verify_device_token

bp = Blueprint("agent", __name__)

# Largest control message the relay will forward. Phase 1 only carries tiny
# JSON envelopes (ping/pong/hello/presence); the cap is defense-in-depth so a
# misbehaving peer can't buffer/forward an oversized message.
_MAX_MESSAGE_BYTES = 65_536


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

    @sock.route("/agent/socket")
    def agent_socket(ws):
        token = request.args.get("token", "")
        device_id = verify_device_token(token)
        if not device_id:
            ws.send(_json.dumps({"v": 1, "type": "error",
                                 "payload": {"reason": "unauthorized"}}))
            return
        touch_device(device_id)
        _device_name = devices.get_device_name(device_id)
        RELAY.register_agent(_ACCOUNT, device_id, ws.send, device_name=_device_name)
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                if len(msg) > _MAX_MESSAGE_BYTES:
                    break
                # Route frames that target the server (event, and future
                # credentials_updated / image_used / pending_results_chunk).
                # on_frame handles unrecognised types as a debug no-op so
                # adding new types in A7/A8/A9 is a small switch addition.
                try:
                    frame = _json.loads(msg)
                    ftype = frame.get("type") if isinstance(frame, dict) else None
                    if ftype in ("event", "credentials_updated",
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
