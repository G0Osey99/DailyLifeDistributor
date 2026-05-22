"""Redeem a pairing code over HTTP and store the resulting device token."""
from __future__ import annotations

import requests

from agent import config


def redeem(server_url: str, code: str, device_name: str, timeout: float = 15.0) -> bool:
    """POST the code to the server; on success store the token + server URL."""
    resp = requests.post(
        server_url.rstrip("/") + "/agent/pair/redeem",
        json={"code": code, "name": device_name},
        timeout=timeout,
    )
    if resp.status_code != 200:
        return False
    token = resp.json().get("token")
    if not token:
        return False
    config.set_token(token)
    config.set_server_url(server_url)
    return True
