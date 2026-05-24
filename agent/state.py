"""Thread-safe state shared between the agent's network loop and its GUI.

The network thread updates this when it changes connection/activity status;
the GUI polls it via `Tk.after()` every ~500ms and repaints. Keeping the
GUI out of the network code (and vice-versa) is the whole point — neither
imports the other.

Pairing-code prompting also routes through here: the network thread sets
``needs_pairing_code = True`` and waits on ``pairing_code_event``; the GUI
shows a modal, stuffs the answer into ``pending_pairing_code``, and sets
the event. CLI mode just calls ``input()`` and never touches the event.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# Connection states the GUI knows how to render.
CONN_STARTING = "starting"      # boot, before first connect
CONN_CONNECTING = "connecting"  # WS handshake in flight
CONN_ONLINE = "online"          # connected + presence registered
CONN_DISCONNECTED = "offline"   # transient drop, will reconnect
CONN_AUTH_FAILED = "auth"       # server rejected the token (re-pair needed)
CONN_STOPPED = "stopped"        # shutdown_event fired

# Activity states.
ACT_IDLE = "idle"
ACT_UPLOADING = "uploading"


@dataclass
class AgentState:
    """Single source of truth for what the GUI displays.

    All setters acquire ``_lock`` so the GUI reading mid-update never
    sees a torn struct. Readers should call ``snapshot()`` to get a
    consistent immutable view in one shot.
    """

    server_url: str = ""
    device_name: str = ""
    hostname: str = ""
    hwid_short: str = ""
    version: str = ""

    connection: str = CONN_STARTING
    activity: str = ACT_IDLE
    activity_detail: str = ""        # e.g. "YouTube · row 3/12"
    last_message: str = ""           # short status banner, e.g. "Re-pair required"

    # Rolling log tail rendered in the GUI's bottom panel.
    log_lines: deque = field(default_factory=lambda: deque(maxlen=200))

    # Pairing-code prompting handshake (GUI mode only).
    needs_pairing_code: bool = False
    pending_pairing_code: Optional[str] = None
    pairing_code_event: threading.Event = field(default_factory=threading.Event)

    # In-memory copy of the agent's current pair token, mirrored from
    # config.set_token() at pair time. Lets the GUI poll authenticated
    # endpoints (like /sessions/status) without round-tripping through
    # the Windows keyring, which is the most common failure mode under
    # PyInstaller — writes succeed, reads return None.
    token: str = ""

    _lock: threading.RLock = field(default_factory=threading.RLock)

    # ---- mutation ----
    def set_connection(self, status: str, message: str = "") -> None:
        with self._lock:
            self.connection = status
            if message:
                self.last_message = message

    def set_activity(self, status: str, detail: str = "") -> None:
        with self._lock:
            self.activity = status
            self.activity_detail = detail

    def set_identity(self, *, device_name: str = "", hostname: str = "",
                     hwid_short: str = "", version: str = "") -> None:
        with self._lock:
            if device_name:
                self.device_name = device_name
            if hostname:
                self.hostname = hostname
            if hwid_short:
                self.hwid_short = hwid_short
            if version:
                self.version = version

    def set_token(self, token: str) -> None:
        """Stash the pair token in memory for GUI-side authenticated polls."""
        with self._lock:
            self.token = (token or "").strip()

    def append_log(self, line: str) -> None:
        with self._lock:
            self.log_lines.append(line.rstrip())

    # ---- read ----
    def snapshot(self) -> dict:
        """Atomic copy of the current UI-facing state."""
        with self._lock:
            return {
                "server_url": self.server_url,
                "device_name": self.device_name,
                "hostname": self.hostname,
                "hwid_short": self.hwid_short,
                "version": self.version,
                "connection": self.connection,
                "activity": self.activity,
                "activity_detail": self.activity_detail,
                "last_message": self.last_message,
                "log_lines": list(self.log_lines),
                "needs_pairing_code": self.needs_pairing_code,
            }

    # ---- pairing-code handshake ----
    def request_pairing_code(self) -> str:
        """Block until the GUI provides a pairing code via ``provide_pairing_code``.

        Called from the network thread. Returns the code string the user
        typed. Raises RuntimeError if the GUI exits without providing one
        (shutdown path).
        """
        with self._lock:
            self.needs_pairing_code = True
            self.pending_pairing_code = None
            self.pairing_code_event.clear()
        # Wait outside the lock so the GUI thread can grab it.
        self.pairing_code_event.wait()
        with self._lock:
            code = self.pending_pairing_code
            self.needs_pairing_code = False
        if not code:
            raise RuntimeError("Pairing cancelled.")
        return code

    def provide_pairing_code(self, code: Optional[str]) -> None:
        """Called from the GUI thread once the user submits / cancels."""
        with self._lock:
            self.pending_pairing_code = code
        self.pairing_code_event.set()
