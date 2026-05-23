"""Phase 1 agent entrypoint: pair if needed, connect, reply pong to ping.

Run:  python -m agent.main --server https://autoalert.pro
First run prompts for a pairing code (generated in the web UI).
"""
from __future__ import annotations

import argparse
import logging
import signal
import socket
import sys
import threading
import time

from agent import config, pair, scan, updater
from agent._version import __version__
from agent.transport import AgentConnection

log = logging.getLogger(__name__)

# Module-level shutdown flag.  Signal handlers set this; the run loop checks it.
_shutdown_event = threading.Event()

# Backoff constants
_RECONNECT_DELAY = 3   # seconds between reconnect attempts
_AUTH_ERR_CODES = {401, 403}


def _device_name() -> str:
    return socket.gethostname() or "device"


def _install_signal_handlers() -> None:
    """Install SIGINT (Ctrl+C) and SIGTERM handlers that set _shutdown_event."""
    def _handle(signum, frame):  # noqa: ANN001
        # Use print here — logging may not be safe inside a signal handler.
        print("\nShutting down...")
        _shutdown_event.set()

    signal.signal(signal.SIGINT, _handle)
    # SIGTERM is POSIX-only; skip gracefully on Windows where it isn't defined.
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle)


def _ensure_paired(server_url: str) -> str:
    token = config.get_token()
    if token:
        return token
    print(f"This device is not paired with {server_url}.")
    code = input("Enter the pairing code shown on the website: ").strip()
    if not pair.redeem(server_url, code, _device_name()):
        raise SystemExit("Pairing failed — check the code and try again.")
    print("✓ Paired successfully.")
    return config.get_token()


def _on_message(conn: AgentConnection, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "ping":
        conn.send({"v": 1, "type": "pong", "payload": msg.get("payload", {})})
    elif mtype == "scan_request":
        log.debug("Handling scan_request")
        report = scan.scan_roots(config.get_media_roots())
        conn.send({"v": 1, "type": "scan_result", "payload": report})
    elif mtype == "job_plan":
        from agent import dispatch

        class _T:
            def send(self, frame):
                conn.send(frame)

        try:
            dispatch.handle_job_plan(plan=msg, transport=_T())
        except Exception as e:
            log.exception("handle_job_plan crashed: %s", e)


def run(server_url: str, shutdown_event: threading.Event | None = None) -> None:
    """Connect to *server_url* and run the message loop until shutdown.

    *shutdown_event* defaults to the module-level ``_shutdown_event`` so that
    signal handlers installed by ``main()`` drive the same flag; tests can
    inject their own event to drive shutdown without touching signal state.
    """
    if shutdown_event is None:
        shutdown_event = _shutdown_event

    token = _ensure_paired(server_url)

    try:
        result = updater.check_and_apply(server_url)
        # check_and_apply calls os._exit() internally on success, so if we
        # reach here the agent is already on the latest version.
        if result is not None:
            # Friendly message for the "update available but couldn't apply"
            # case (frozen=False dev run or verify failed).
            log.debug("update check complete; no restart needed")
    except Exception:
        log.debug("update check raised; continuing", exc_info=True)

    print(f"Connecting to {server_url}...")
    print("Press Ctrl+C to stop.")

    consecutive_auth_failures = 0

    while not shutdown_event.is_set():
        conn = AgentConnection(server_url, token, shutdown_event=shutdown_event)
        try:
            conn.connect()
            print(f"✓ Connected ({_device_name()})")
            consecutive_auth_failures = 0
            # Bind conn into the callback's default arg so the closure can't
            # accidentally pick up a later iteration's connection.
            while conn.run_once(lambda m, c=conn: _on_message(c, m)):
                pass
            if not shutdown_event.is_set():
                log.debug("Connection closed by server; will reconnect")
        except OSError as exc:
            if shutdown_event.is_set():
                break
            log.debug("Network error: %s", exc, exc_info=True)
            print(f"Couldn't reach {server_url}. Check your internet connection. "
                  f"Retrying in {_RECONNECT_DELAY}s...")
        except Exception as exc:  # noqa: BLE001
            if shutdown_event.is_set():
                break
            # Detect auth errors from simple_websocket (raised as exceptions
            # with status codes embedded in the message).
            msg_str = str(exc)
            is_auth = any(str(c) in msg_str for c in _AUTH_ERR_CODES)
            if is_auth:
                consecutive_auth_failures += 1
                if consecutive_auth_failures >= 2:
                    print(
                        "This device's pairing has been revoked or expired. "
                        "Re-pair from the website and restart the agent."
                    )
                    break
            log.debug("agent connection dropped; reconnecting", exc_info=True)
        finally:
            conn.close()

        if shutdown_event.is_set():
            break
        # Wait for the reconnect delay but wake immediately if shutdown fires.
        shutdown_event.wait(timeout=_RECONNECT_DELAY)

    print("Goodbye!")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="DLD hybrid upload agent",
    )
    ap.add_argument(
        "--server",
        default=config.get_server_url() or "https://autoalert.pro",
        help="Server URL (default: autoalert.pro)",
    )
    ap.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed debug output",
    )
    ap.add_argument(
        "--version",
        action="version",
        version=f"dld-agent {__version__}",
    )
    args = ap.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    _install_signal_handlers()
    run(args.server)


if __name__ == "__main__":
    main()
