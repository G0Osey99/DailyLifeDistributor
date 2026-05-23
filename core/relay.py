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
        # Tell the freshly-connected browser the current agent status, so a
        # browser that connects while an agent is already online learns it
        # immediately (not only on the next agent connect/disconnect).
        online = self.agent_online(account)
        sink(json.dumps({"v": 1, "type": "presence", "payload": {"online": online}}))

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


# ---------------------------------------------------------------------------
# Module-level default relay + convenience send function
# ---------------------------------------------------------------------------
# blueprints/agent.py calls set_default_relay(RELAY) after creating its
# process-wide Relay instance, so agent_dispatch can call send_to_device
# without importing blueprints (which would create a circular dependency).

_default_relay: "Relay | None" = None
_default_account: str = "default"


def set_default_relay(relay: "Relay", account: str = "default") -> None:
    """Register the process-wide Relay instance used by send_to_device."""
    global _default_relay, _default_account
    _default_relay = relay
    _default_account = account


def send_to_device(device_name: str, envelope: dict) -> None:
    """Send *envelope* (serialised to JSON) to every agent socket whose
    device_id matches *device_name*.

    In the single-account deployment device_name == device_id (hex UUID).
    The relay stores agents keyed by device_id; we broadcast to all agents
    whose key equals device_name so the caller can pass either form.

    Raises RuntimeError if no default relay has been set (i.e. the blueprint
    has not been registered), or ValueError if the named device is not
    currently connected.  In tests the function is monkeypatched, so neither
    path fires during unit testing.
    """
    if _default_relay is None:
        raise RuntimeError("send_to_device: no default relay set; "
                           "call relay.set_default_relay() first")
    msg = json.dumps(envelope)
    with _default_relay._lock:
        room = _default_relay._rooms.get(_default_account)
        sink = room.agents.get(device_name) if room else None
    if sink is None:
        raise ValueError(f"send_to_device: device {device_name!r} not connected")
    sink(msg)
