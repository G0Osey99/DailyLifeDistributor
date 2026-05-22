"""In-memory relay hub joining browsers and agents into account-scoped rooms.

A `sink` is any callable taking one str (the raw JSON message) — in production
it wraps a flask-sock connection's .send; in tests it's a list-appender. The
relay never parses message bodies except to stamp presence; it just forwards.
"""
from __future__ import annotations

import json
import threading
from typing import Callable

Sink = Callable[[str], None]


class _Room:
    def __init__(self) -> None:
        self.agents: dict[str, Sink] = {}      # device_id -> sink
        self.browsers: dict[str, Sink] = {}    # session_id -> sink


class Relay:
    def __init__(self) -> None:
        self._rooms: dict[str, _Room] = {}
        self._lock = threading.RLock()

    def _room(self, account: str) -> _Room:
        return self._rooms.setdefault(account, _Room())

    # ---- registration -------------------------------------------------
    def register_agent(self, account: str, device_id: str, sink: Sink) -> None:
        with self._lock:
            self._room(account).agents[device_id] = sink
        self._broadcast_presence(account, online=True)

    def unregister_agent(self, account: str, device_id: str) -> None:
        with self._lock:
            self._room(account).agents.pop(device_id, None)
            still_online = bool(self._room(account).agents)
        self._broadcast_presence(account, online=still_online)

    def register_browser(self, account: str, session_id: str, sink: Sink) -> None:
        with self._lock:
            self._room(account).browsers[session_id] = sink

    def unregister_browser(self, account: str, session_id: str) -> None:
        with self._lock:
            self._room(account).browsers.pop(session_id, None)

    # ---- routing ------------------------------------------------------
    def route_from_browser(self, account: str, message: str) -> None:
        with self._lock:
            sinks = list(self._room(account).agents.values())
        for s in sinks:
            s(message)

    def route_from_agent(self, account: str, message: str) -> None:
        with self._lock:
            sinks = list(self._room(account).browsers.values())
        for s in sinks:
            s(message)

    # ---- presence -----------------------------------------------------
    def agent_online(self, account: str) -> bool:
        with self._lock:
            return bool(self._rooms.get(account) and self._rooms[account].agents)

    def _broadcast_presence(self, account: str, online: bool) -> None:
        msg = json.dumps({"v": 1, "type": "presence", "payload": {"online": online}})
        with self._lock:
            sinks = list(self._room(account).browsers.values())
        for s in sinks:
            s(msg)
