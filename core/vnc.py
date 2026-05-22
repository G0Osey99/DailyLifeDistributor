"""Hosted-only x11vnc lifecycle — one fresh VNC password per login session.

deploy/start.sh runs Xvfb + websockify (→ :5900) for the whole container; x11vnc
itself is started and stopped *here*, per remote-login session, each time with a
newly-generated VNC password handed to noVNC. When no session is active no
x11vnc runs, so an unauthenticated hit on /vnc-ws has nothing to connect to —
and a leaked password is useless beyond the one session it belonged to.

Off the hosted VPS this is a no-op (local dev has no Xvfb/x11vnc).
"""
from __future__ import annotations

import logging
import os
import secrets
import string
import subprocess
import threading

from core.hosted import is_hosted

log = logging.getLogger(__name__)

_DISPLAY = os.environ.get("DISPLAY", ":99")
_RFB_AUTH = os.environ.get("VNC_AUTH_FILE", "/data/vnc_passwd.rfb")
_RFB_PORT = "5900"

_lock = threading.Lock()
_proc: "subprocess.Popen | None" = None
_password = ""


def _gen_password() -> str:
    # VNC auth (DES) only uses the first 8 chars; 8 random alphanumerics is the
    # full effective strength.
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def current_password() -> str:
    with _lock:
        return _password


def start_session() -> str:
    """(Re)start x11vnc with a fresh password and return it.

    No-op (returns "") off the hosted VPS. Raises if x11vnc can't be started so
    the caller can abort the login cleanly.
    """
    global _proc, _password
    if not is_hosted():
        return ""
    with _lock:
        _stop_locked()
        pw = _gen_password()
        subprocess.run(
            ["x11vnc", "-storepasswd", pw, _RFB_AUTH],
            check=True, capture_output=True, timeout=10,
        )
        _proc = subprocess.Popen(
            ["x11vnc", "-display", _DISPLAY, "-localhost", "-rfbauth", _RFB_AUTH,
             "-forever", "-shared", "-rfbport", _RFB_PORT, "-quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _password = pw
        log.info("x11vnc started for remote-login session (pid=%s)", _proc.pid)
        return _password


def stop_session() -> None:
    """Stop x11vnc and forget the password — the stream is dead until the next
    start_session(). Safe to call repeatedly / off-hosted."""
    global _password
    if not is_hosted():
        return
    with _lock:
        _stop_locked()
        _password = ""


def _stop_locked() -> None:
    global _proc
    if _proc is not None:
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception as e:  # noqa: BLE001 — escalate to kill
            log.debug("x11vnc terminate failed, killing: %s", e)
            try:
                _proc.kill()
            except Exception as ke:  # noqa: BLE001 — best-effort
                log.debug("x11vnc kill failed: %s", ke)
        _proc = None
    # Belt-and-suspenders: clear any stray x11vnc bound to our port.
    try:
        subprocess.run(["pkill", "-f", f"x11vnc.*-rfbport {_RFB_PORT}"],
                       capture_output=True, timeout=5)
    except Exception as e:  # noqa: BLE001 — pkill may be absent / nothing to kill
        log.debug("x11vnc pkill sweep failed: %s", e)
