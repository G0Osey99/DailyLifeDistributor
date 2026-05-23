"""Agent-side config: server URL + media roots in a JSON file; device token in
the OS keychain (via keyring). The token never touches the JSON file."""
from __future__ import annotations

import json
import os

import keyring as _keyring

_SERVICE = "dld-hybrid-agent"
_TOKEN_USER = "device-token"
_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".dld-agent", "agent.json")


def _load() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def set_token(token: str) -> None:
    _keyring.set_password(_SERVICE, _TOKEN_USER, token)


def get_token() -> str | None:
    return _keyring.get_password(_SERVICE, _TOKEN_USER)


def clear_token() -> None:
    try:
        _keyring.delete_password(_SERVICE, _TOKEN_USER)
    except Exception:
        pass


def set_server_url(url: str) -> None:
    d = _load(); d["server_url"] = url.rstrip("/"); _save(d)


def get_server_url() -> str | None:
    return _load().get("server_url")


def set_media_roots(roots: dict) -> None:
    d = _load(); d["media_roots"] = roots; _save(d)


def get_media_roots() -> dict:
    return _load().get("media_roots", {})


def set_device_id(device_id: str) -> None:
    """Persist the server-assigned device_id from the pairing response.

    Used by the whoami_ping/pong protocol so the agent can report its own
    identity to the browser without a server roundtrip. Stored in the
    plain JSON config file (no secret value).
    """
    d = _load(); d["device_id"] = device_id; _save(d)


def get_device_id() -> str | None:
    return _load().get("device_id")
