"""Outbound wss client to the VPS relay. Sends a hello handshake, then a
receive loop that hands each decoded message to a callback. Reconnect with
backoff is the caller's concern via connect()/run_once()."""
from __future__ import annotations

import json
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import simple_websocket

PROTOCOL_VERSION = 1


def _connect(url: str):
    """Seam for tests: real WebSocket client."""
    return simple_websocket.Client(url)


def _to_ws_url(server_url: str, token: str) -> str:
    parts = urlsplit(server_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, "/agent/socket", f"token={token}", ""))


class AgentConnection:
    def __init__(self, server_url: str, token: str):
        self.server_url = server_url
        self.token = token
        self.ws = None

    def connect(self) -> None:
        self.ws = _connect(_to_ws_url(self.server_url, self.token))
        hello = json.dumps({"v": PROTOCOL_VERSION, "type": "hello",
                            "payload": {"role": "agent"}})
        if hasattr(self.ws, "send"):
            self.ws.send(hello)

    def send(self, message: dict) -> None:
        self.ws.send(json.dumps(message))

    def run_once(self, on_message: Callable[[dict], None]) -> bool:
        """Receive one message and dispatch it. Returns False when closed."""
        raw = self.ws.receive(timeout=None)
        if raw is None:
            return False
        on_message(json.loads(raw))
        return True

    def close(self) -> None:
        if self.ws:
            self.ws.close()
