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
