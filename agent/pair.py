"""Redeem a pairing code over HTTP and store the resulting device token."""
from __future__ import annotations

import requests

from agent import config


def redeem(
    server_url: str,
    code: str,
    device_name: str,
    timeout: float = 15.0,
    *,
    hwid_hash: str | None = None,
    hostname: str | None = None,
) -> bool:
    """POST the code to the server; on success store the token + server URL.

    *hwid_hash* and *hostname* are optional metadata sent on the redeem
    request so the server can render a meaningful device picker. Older
    servers ignore extra JSON fields (Flask request.get_json with the
    default silent=True), so this stays backward compatible. Both
    arguments default to None for tests that don't care to set them.
    """
    body: dict[str, object] = {"code": code, "name": device_name}
    if hwid_hash:
        body["hwid_hash"] = hwid_hash
    if hostname:
        body["hostname"] = hostname
    resp = requests.post(
        server_url.rstrip("/") + "/agent/pair/redeem",
        json=body,
        timeout=timeout,
    )
    if resp.status_code != 200:
        return False
    payload = resp.json()
    token = payload.get("token")
    if not token:
        return False
    config.set_token(token)
    config.set_server_url(server_url)
    # Persist the device_id so whoami_pong can self-identify without a
    # server roundtrip. Older servers omit the field; we treat that as
    # "no device_id known locally" and fall back to None at pong time.
    device_id = (payload.get("device_id") or "").strip()
    if device_id:
        config.set_device_id(device_id)
    return True
