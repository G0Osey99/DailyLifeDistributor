"""Phase 1 agent entrypoint: pair if needed, connect, reply pong to ping.

Run:  python -m agent.main --server https://autoalert.pro
First run prompts for a pairing code (generated in the web UI).
"""
from __future__ import annotations

# --- Crash-safe boot logging ---------------------------------------------
#
# Imports below have, historically, been the most common source of
# silent-crash-on-start. configure_logging() only fires after main() begins,
# so any failure during module import was invisible — the agent would exit
# before a single log line landed on disk.
#
# This block writes a one-liner to a boot trace file BEFORE any other
# package imports so we always know:
#   1. that main.py was reached,
#   2. the Python/OS we're under,
#   3. the traceback if any subsequent import or main() call blows up.
#
# Boot trace lives at: <user-log-dir>/boot.log (rotated by length, never
# truncated on each launch).
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _boot_log_path() -> Path:
    """Resolve the boot-trace path independent of logging or platformdirs."""
    try:
        import platformdirs
        base = Path(platformdirs.user_log_dir("dld-agent", appauthor=False))
    except Exception:
        base = Path.home() / ".dld-agent" / "logs"
    return base / "boot.log"


def _boot_write(message: str) -> None:
    """Append a one-line trace to boot.log. Best-effort — never raises.

    Trim the file at ~1 MB so it doesn't grow forever on a crash-loop.
    """
    try:
        p = _boot_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} {message}\n")
            fh.flush()
        # Cheap size-cap: if the file gets large, truncate to last ~500 KB.
        if p.stat().st_size > 1_000_000:
            data = p.read_bytes()[-500_000:]
            p.write_bytes(data)
    except Exception:
        pass


_boot_write(
    f"=== agent boot pid={__import__('os').getpid()} "
    f"python={sys.version.split()[0]} "
    f"platform={sys.platform} "
    f"argv={sys.argv!r}"
)


# Install faulthandler EARLY so a C-level segfault (PyInstaller, native
# extension, Playwright bootstrap, etc.) dumps a stack to stderr + the
# boot log instead of dying silently. Best-effort: any failure here must
# not prevent the agent from starting.
try:
    import faulthandler
    _fh_stream = open(_boot_log_path(), "a", encoding="utf-8")
    faulthandler.enable(file=_fh_stream)
except Exception:
    pass


import argparse
import logging
import logging.handlers
import os
import signal
import socket
import threading
import time
from typing import Optional

try:
    from agent import config, hostname as _hostname_mod, hwid as _hwid_mod, pair, scan, updater
    from agent._version import __version__
    from agent.transport import AgentConnection
except Exception:
    _boot_write("IMPORT FAILED:\n" + traceback.format_exc())
    raise

log = logging.getLogger(__name__)


# --- File logging setup -----------------------------------------------------
#
# stdout-only logging was easy to lose: any user who closed the terminal,
# any forwarded-port session that timed out, any system reboot, any agent
# crash — gone. The file handler gives us ~50 MB of rolling history at
# INFO+ regardless of --verbose so on-call has something to read.
#
# --verbose elevates BOTH the stdout AND the file handler to DEBUG.

_LOG_FILENAME = "agent.log"
_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_LOG_BACKUP_COUNT = 5  # 5 rotations × 10 MB = ~50 MB ceiling


def _default_log_dir() -> Path:
    """Pick a cross-platform user data dir for agent logs.

    Order:
      1. platformdirs.user_log_dir("dld-agent") if available (proper
         per-OS conventions: ~/Library/Logs on macOS, %LOCALAPPDATA%
         on Windows, ~/.cache on Linux).
      2. Fallback to ~/.dld-agent/logs for environments without
         platformdirs installed.
    """
    try:
        import platformdirs
        return Path(platformdirs.user_log_dir("dld-agent", appauthor=False))
    except Exception:
        return Path.home() / ".dld-agent" / "logs"


def configure_logging(*, log_dir: Optional[str] = None,
                      verbose: bool = False) -> Path:
    """Set up file + stdout logging.

    Always logs INFO+ to ``<log_dir>/agent.log`` with rotation (10 MB × 5);
    --verbose elevates both handlers to DEBUG so an on-call user can
    grab a full debug trace by re-running with ``-v``.

    Returns the resolved log directory (useful for tests and for printing
    "Logs: <path>" at startup).

    Idempotent: removing pre-existing handlers prevents double-attachment
    if this is called twice (e.g. tests that import main multiple times).
    """
    resolved_dir = Path(log_dir) if log_dir else _default_log_dir()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    log_path = resolved_dir / _LOG_FILENAME

    root = logging.getLogger()
    # Clear any handlers already attached (basicConfig leaves a StreamHandler
    # behind; we want exactly the two handlers we install below).
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_level = logging.DEBUG if verbose else logging.INFO
    stream_level = logging.DEBUG if verbose else logging.WARNING
    # Root level must be at-or-below the most permissive handler level.
    root.setLevel(min(file_level, stream_level))

    file_handler = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    # Non-verbose stdout default was WARNING; keep that so the console
    # stays uncluttered. --verbose lifts both handlers together.
    stream_handler.setLevel(stream_level)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    return resolved_dir

# Module-level shutdown flag.  Signal handlers set this; the run loop checks it.
_shutdown_event = threading.Event()

# Backoff constants
_RECONNECT_DELAY = 3   # seconds between reconnect attempts
_AUTH_ERR_CODES = {401, 403}

# Single-job invariant: the agent runs one upload at a time. Track the
# currently-active job_id so a second job_plan frame is rejected cleanly
# instead of racing the first. Per-job state lives in run_batch.
_active_job_lock = threading.Lock()
_active_job_id: str | None = None


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
    # Compute HWID + friendly hostname locally so the server can render
    # a meaningful device picker. compute_hwid_hash() never raises (it
    # falls back to a hostname-derived seed); get_friendly_hostname()
    # always returns a non-empty string.
    hwid_hash = _hwid_mod.compute_hwid_hash()
    friendly = _hostname_mod.get_friendly_hostname()
    result = pair.redeem(server_url, code, _device_name(),
                         hwid_hash=hwid_hash, hostname=friendly)
    if not result:
        raise SystemExit("Pairing failed — check the code and try again.")
    if isinstance(result, dict) and result.get("relinked"):
        prev = result.get("previous_name") or _device_name()
        print(f"✓ Re-linked to {prev} (replaced a prior pairing on this hardware).")
    else:
        print("✓ Paired successfully.")
    return config.get_token()


def _on_message(conn: AgentConnection, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "ping":
        conn.send({"v": 1, "type": "pong", "payload": msg.get("payload", {})})
    elif mtype == "whoami_ping":
        # Phase 3.5 device picker confirmation. The browser sends a ping_id
        # and we echo our local identity so the dashboard chip can refresh
        # the displayed hostname / HWID without waiting for a DB reread.
        # If something goes wrong gathering the local data (very unlikely —
        # hostname always returns a string; HWID falls back to the seed),
        # we still emit a pong with whatever we have rather than dropping
        # the ping silently — the browser is waiting on ping_id.
        try:
            hwid_hash = _hwid_mod.compute_hwid_hash()
        except Exception:  # noqa: BLE001
            hwid_hash = ""
        try:
            host = _hostname_mod.get_friendly_hostname()
        except Exception:  # noqa: BLE001
            host = ""
        conn.send({
            "v": 1,
            "type": "whoami_pong",
            "ping_id": msg.get("ping_id", ""),
            "device_id": config.get_device_id() or "",
            "hwid_hash": hwid_hash,
            "hostname": host,
            "protocol_version": __version__,
        })
    elif mtype == "scan_request":
        log.debug("Handling scan_request")
        report = scan.scan_roots(config.get_media_roots())
        conn.send({"v": 1, "type": "scan_result", "payload": report})
    elif mtype == "cancel_job":
        # Server forwarded a user-initiated cancel for a specific job_id.
        # In-flight uploads keep running (cooperative cancel); pending
        # rows short-circuit with an error_type=cancelled event.
        from agent import dispatch as _dispatch_mod
        target = msg.get("job_id", "")
        ok = _dispatch_mod.signal_cancel(target) if target else False
        if not ok:
            log.debug(
                "cancel_job received for unknown job_id=%s",
                (target or "<missing>")[:8],
            )
    elif mtype == "job_plan":
        # Run the job on a daemon thread so the receive loop stays
        # responsive — Cloudflare/nginx idle ~100s would otherwise drop
        # the WebSocket mid-upload because the recv loop was blocked
        # on handle_job_plan (minutes-to-hours).
        from agent import dispatch

        job_id = msg.get("job_id", "")

        class _T:
            def send(self, frame):
                conn.send(frame)

        transport_wrapper = _T()

        # Single-job invariant: reject a second job_plan while one is
        # already running. The server normally serializes per-agent, so
        # this is a safety net.
        global _active_job_id
        with _active_job_lock:
            if _active_job_id is not None:
                log.warning(
                    "Rejecting job_plan %s — agent busy with job %s",
                    job_id[:8] if job_id else "?",
                    _active_job_id[:8] if _active_job_id else "?",
                )
                try:
                    transport_wrapper.send({
                        "v": 1,
                        "type": "event",
                        "event": "error",
                        "job_id": job_id,
                        "error": f"agent busy with job {_active_job_id}",
                    })
                except Exception:
                    log.debug("busy-rejection send failed", exc_info=True)
                return
            _active_job_id = job_id or "<unknown>"

        def _run_plan():
            global _active_job_id
            try:
                dispatch.handle_job_plan(plan=msg, transport=transport_wrapper)
            except Exception as e:
                log.exception("handle_job_plan crashed: %s", e)
            finally:
                with _active_job_lock:
                    _active_job_id = None

        threading.Thread(
            target=_run_plan,
            daemon=True,
            name=f"job-{(job_id or 'unknown')[:8]}",
        ).start()


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
            # Detect auth errors. simple_websocket raises ConnectionError
            # with a ``status_code`` attribute on a non-101 HTTP response
            # (verified via inspect on simple_websocket.errors.ConnectionError);
            # prefer that over a substring match. Fall back to a tightened
            # word-boundary string match for unexpected exception types so
            # we don't mis-count a "401" embedded in a hostname or message.
            is_auth = False
            status_code = getattr(exc, "status_code", None)
            if isinstance(status_code, int) and status_code in _AUTH_ERR_CODES:
                is_auth = True
            else:
                import re
                if re.search(r"\b(401|403)\b", str(exc)):
                    # Heuristic — older simple_websocket versions don't set
                    # status_code reliably; the only signal is the message
                    # body. Tightened to word boundaries so a port like
                    # 4011 doesn't trigger a false positive.
                    is_auth = True
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
        help="Show detailed debug output (file + stdout both go to DEBUG)",
    )
    ap.add_argument(
        "--log-dir",
        default=None,
        help=(
            "Directory for rotating agent.log files. "
            "Default: platformdirs user_log_dir, or ~/.dld-agent/logs."
        ),
    )
    ap.add_argument(
        "--version",
        action="version",
        version=f"dld-agent {__version__}",
    )
    args = ap.parse_args()

    log_path = configure_logging(log_dir=args.log_dir, verbose=args.verbose)
    print(f"Logs: {log_path}")
    print(f"Boot trace: {_boot_log_path()}")
    _boot_write(f"main: configure_logging ok, log_dir={log_path}")

    _install_signal_handlers()
    run(args.server)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        _boot_write(f"main: SystemExit code={e.code}")
        raise
    except KeyboardInterrupt:
        _boot_write("main: KeyboardInterrupt — clean shutdown")
        raise
    except BaseException:
        # Catch BaseException (not just Exception) so SystemExit doesn't slip
        # past — we already handled SystemExit above. Anything else means the
        # agent died unexpectedly; record the trace to boot.log so the
        # user can see what happened.
        _boot_write("main: UNCAUGHT EXCEPTION:\n" + traceback.format_exc())
        raise
