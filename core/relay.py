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
        self.agents: dict[str, Sink] = {}           # device_id -> sink
        self.browsers: dict[str, Sink] = {}         # session_id -> sink
        self.agent_names: dict[str, str] = {}       # device_id -> device_name
        self.agent_ips: dict[str, str] = {}         # device_id -> connect_ip


class Relay:
    def __init__(self) -> None:
        self._rooms: dict[str, _Room] = {}
        self._lock = threading.RLock()

    def _room(self, account: str) -> _Room:
        return self._rooms.setdefault(account, _Room())

    # ---- registration -------------------------------------------------
    def register_agent(self, account: str, device_id: str, sink: Sink,
                       device_name: str | None = None,
                       connect_ip: str | None = None) -> None:
        """Register an agent's WebSocket sink in the account-scoped room.

        *connect_ip* is the public IP the agent connected from. Stored
        alongside the sink so the dashboard can annotate same_network for
        the device picker (a browser whose CF-Connecting-IP matches the
        agent's stored IP is on the same LAN/NAT).
        """
        with self._lock:
            room = self._room(account)
            room.agents[device_id] = sink
            if device_name:
                room.agent_names[device_id] = device_name
            if connect_ip:
                room.agent_ips[device_id] = connect_ip
        self._broadcast_presence(account, online=True)

    def unregister_agent(self, account: str, device_id: str) -> None:
        with self._lock:
            room = self._room(account)
            room.agents.pop(device_id, None)
            room.agent_names.pop(device_id, None)
            room.agent_ips.pop(device_id, None)
            still_online = bool(room.agents)
        self._broadcast_presence(account, online=still_online)

    # ---- introspection ------------------------------------------------
    def online_agents(self, account: str) -> list[dict]:
        """Return a snapshot of currently-online agents in *account*.

        Each entry: ``{"device_id": str, "device_name": str | None,
        "connect_ip": str | None}``. Safe to iterate without holding the
        relay lock — the returned list is a copy.
        """
        with self._lock:
            room = self._rooms.get(account)
            if not room:
                return []
            return [
                {
                    "device_id": did,
                    "device_name": room.agent_names.get(did),
                    "connect_ip": room.agent_ips.get(did),
                }
                for did in room.agents
            ]

    def agent_ip(self, account: str, device_id: str) -> str | None:
        """Return the stored connect IP for *device_id*, or None."""
        with self._lock:
            room = self._rooms.get(account)
            if not room:
                return None
            return room.agent_ips.get(device_id)

    def register_browser(self, account: str, session_id: str, sink: Sink) -> None:
        with self._lock:
            self._room(account).browsers[session_id] = sink
        # Tell the freshly-connected browser the current agent status, so a
        # browser that connects while an agent is already online learns it
        # immediately (not only on the next agent connect/disconnect).
        payload = self._presence_payload(account)
        sink(json.dumps({"v": 1, "type": "presence", "payload": payload}))

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

    def _presence_payload(self, account: str) -> dict:
        """Build the presence payload dict for *account*.

        Includes ``online`` (bool) and, when at least one agent is connected,
        ``device_name`` (the name of the most-recently-registered agent).
        """
        with self._lock:
            room = self._rooms.get(account)
            if room and room.agents:
                # Most-recently-registered agent is last in insertion-order dict.
                last_id = next(reversed(room.agents))
                name = room.agent_names.get(last_id)
                payload: dict = {"online": True}
                if name:
                    payload["device_name"] = name
                return payload
        return {"online": False}

    def _broadcast_presence(self, account: str, online: bool) -> None:
        payload = self._presence_payload(account)
        msg = json.dumps({"v": 1, "type": "presence", "payload": payload})
        with self._lock:
            sinks = list(self._room(account).browsers.values())
        for s in sinks:
            s(msg)

    def broadcast_to_browsers(self, account: str, event_type: str,
                              payload: dict) -> None:
        """Send a server-originated frame to every browser socket in *account*.

        Used for one-off notifications the dashboard needs to surface (e.g.
        a paired agent re-linking onto an existing HWID). Frame shape
        matches the rest of the protocol: ``{"v": 1, "type": <event_type>,
        "payload": {...}}``. The sink list is snapshotted under the lock so
        a slow sink (or a sink that disconnects mid-broadcast) can't hold
        the lock or break iteration for the other browsers.
        """
        msg = json.dumps({"v": 1, "type": event_type, "payload": payload})
        with self._lock:
            sinks = list(self._room(account).browsers.values())
        for s in sinks:
            try:
                s(msg)
            except Exception:
                # A single broken sink mustn't take the broadcast down for
                # the rest of the room. The websocket layer reaps closed
                # connections on its own loop.
                pass


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
