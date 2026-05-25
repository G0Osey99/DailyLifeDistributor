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
import signal
import socket
import threading
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

# Module-level GUI state bridge. Set by main() when GUI mode is enabled.
# When non-None, _ensure_paired routes the pairing prompt through the GUI
# instead of stdin, and the run loop streams connection-status changes
# into it. CLI mode (--no-gui) leaves it None and the agent behaves as
# it always did.
from agent import state as _st  # noqa: E402
from agent.state import AgentState as _AgentState  # noqa: E402
_state: "_AgentState | None" = None

# Backoff constants for the reconnect loop. The base is 3s (what we used
# to have, hard-coded), the ceiling is 60s, and the exponent doubles each
# consecutive failure — so a relay outage gets probed at 3, 6, 12, 24, 48,
# then 60s forever, with ±20% jitter on every attempt to avoid a thundering
# herd when many paired agents try to reconnect at the same moment after a
# server restart. A successful connection resets the counter.
_RECONNECT_BASE_DELAY = 3.0
_RECONNECT_MAX_DELAY = 60.0
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


def _prompt_pairing_code(server_url: str) -> str:
    """Get a pairing code from the user — GUI modal or stdin."""
    if _state is not None:
        return _state.request_pairing_code()
    print(f"This device is not paired with {server_url}.")
    return input("Enter the pairing code shown on the website: ").strip()


def _ensure_paired(server_url: str) -> str:
    token = config.get_token()
    if token:
        return token
    code = _prompt_pairing_code(server_url).strip()
    if not code:
        raise SystemExit("Pairing cancelled.")
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
        msg = f"Re-linked to {prev} (replaced a prior pairing on this hardware)."
    else:
        msg = "Paired successfully."
    print(f"✓ {msg}")
    if _state is not None:
        _state.append_log(msg)
    return config.get_token()


# Set by _on_message when the server tells us the token is dead. The run loop
# checks this between iterations and tears down to a re-pair prompt.
_token_revoked_event = threading.Event()


def _on_message(conn: AgentConnection, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "error":
        # Server-sent in-band error. The relay does NOT reject the WebSocket
        # handshake on a revoked token — it accepts the upgrade, then sends
        # {type:"error", payload:{reason:"unauthorized"}}, then closes. So
        # the only signal of revocation is this message; treat any payload
        # reason in {unauthorized, revoked} as "clear the token and re-pair".
        reason = ""
        if isinstance(msg.get("payload"), dict):
            reason = str(msg["payload"].get("reason", "")).lower()
        if reason in ("unauthorized", "revoked", "device_revoked"):
            log.warning(
                "Server reported the agent token is no longer valid (%s); "
                "clearing local token and exiting so the next run re-pairs.",
                reason,
            )
            try:
                config.clear_token()
            except Exception:
                log.debug("clear_token failed", exc_info=True)
            _token_revoked_event.set()
            _shutdown_event.set()
            return
        # Any other server-side error: surface to the user and keep going.
        log.warning("Server error: %s", msg)
        return
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
            if _state is not None:
                _state.set_activity(_st.ACT_UPLOADING)
            try:
                dispatch.handle_job_plan(plan=msg, transport=transport_wrapper)
            except Exception as e:
                log.exception("handle_job_plan crashed: %s", e)
            finally:
                with _active_job_lock:
                    _active_job_id = None
                if _state is not None:
                    _state.set_activity(_st.ACT_IDLE)

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

    if _state is not None:
        _state.server_url = server_url
        _state.set_identity(
            device_name=_device_name(),
            hostname=_hostname_mod.get_friendly_hostname(),
            hwid_short=(_hwid_mod.compute_hwid_hash() or "")[:8],
            version=__version__,
        )
        _state.set_connection(_st.CONN_STARTING)

    token = _ensure_paired(server_url)
    # Mirror the token onto AgentState so the GUI can poll authenticated
    # endpoints (/sessions/status) without round-tripping through the
    # Windows keyring. Keyring under PyInstaller is fragile — writes
    # succeed, reads sometimes return None and the sessions panel goes
    # dark. The state copy is the reliable path.
    if _state is not None:
        _state.set_token(token or "")

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
    consecutive_connect_failures = 0

    while not shutdown_event.is_set():
        if _state is not None:
            _state.set_connection(_st.CONN_CONNECTING)
        conn = AgentConnection(server_url, token, shutdown_event=shutdown_event)
        try:
            log.info("agent: opening WebSocket to %s", server_url)
            conn.connect()
            log.info("agent: WebSocket connected as %s", _device_name())
            print(f"✓ Connected ({_device_name()})")
            if _state is not None:
                _state.set_connection(_st.CONN_ONLINE)
                _state.append_log(f"Connected as {_device_name()}")
            consecutive_auth_failures = 0
            consecutive_connect_failures = 0
            # Bind conn into the callback's default arg so the closure can't
            # accidentally pick up a later iteration's connection.
            while conn.run_once(lambda m, c=conn: _on_message(c, m)):
                pass
            if not shutdown_event.is_set():
                log.debug("Connection closed by server; will reconnect")
        except OSError as exc:
            if shutdown_event.is_set():
                break
            # WARNING (not DEBUG) so the log file captures evidence of a
            # failing connect — at DEBUG the user saw zero log output and
            # the GUI sat at "Connecting…" forever (the symptom that
            # surfaced the certifi/TLS bug in v0.7.0). Include the
            # exception type explicitly because the message alone can be
            # cryptic ("[Errno 60] Operation timed out" doesn't tell you
            # whether it's TCP, TLS handshake, or read).
            log.warning(
                "agent connect failed (%s): %s — will retry with backoff",
                type(exc).__name__, exc,
            )
            # Update the GUI state too — the OSError branch used to leave
            # the state stuck at CONN_CONNECTING, which the user reads as
            # "the agent is still trying" when really it's bounced and is
            # waiting to retry. CONN_DISCONNECTED renders as "Reconnecting…"
            # in the GUI, which matches the actual behavior.
            if _state is not None:
                _state.set_connection(
                    _st.CONN_DISCONNECTED,
                    message=f"Couldn't reach {server_url} — retrying",
                )
            # Backoff is computed at the bottom of the loop from
            # consecutive_connect_failures; capped at _RECONNECT_MAX_DELAY.
            print(f"Couldn't reach {server_url}. Check your internet connection. "
                  f"Retrying shortly (up to {_RECONNECT_MAX_DELAY:.0f}s)...")
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
                    if _state is not None:
                        _state.set_connection(
                            _st.CONN_AUTH_FAILED,
                            message="Re-pair from the website",
                        )
                    break
            log.debug("agent connection dropped; reconnecting", exc_info=True)
            if _state is not None:
                _state.set_connection(_st.CONN_DISCONNECTED)
        finally:
            conn.close()

        # If the server told us this device is revoked, _on_message cleared
        # the saved token and set both _token_revoked_event and
        # shutdown_event. Catch that here, re-prompt for a new pairing code,
        # and resume the connect loop with the fresh token — no restart
        # needed.
        if _token_revoked_event.is_set():
            print(
                "\nThis device's pairing was revoked on the server. "
                "Enter a new pairing code to continue."
            )
            if _state is not None:
                _state.set_connection(
                    _st.CONN_AUTH_FAILED,
                    message="Pairing revoked — enter a new code",
                )
            _token_revoked_event.clear()
            shutdown_event.clear()
            try:
                token = _ensure_paired(server_url)
            except SystemExit as e:
                print(str(e) if e.code else "Pairing failed.")
                break
            # Refresh the in-memory token on AgentState so the GUI's
            # sessions poller picks up the new credential immediately
            # instead of waiting for a process restart to re-read the
            # keyring.
            if _state is not None:
                _state.set_token(token or "")
            continue

        if shutdown_event.is_set():
            break
        # Exponential backoff with jitter — see _RECONNECT_BASE_DELAY /
        # _RECONNECT_MAX_DELAY at module top. consecutive_connect_failures
        # is incremented here (after a failed attempt) and reset to 0
        # immediately upon a successful connect() above.
        consecutive_connect_failures += 1
        import random as _random
        backoff = min(
            _RECONNECT_BASE_DELAY * (2 ** (consecutive_connect_failures - 1)),
            _RECONNECT_MAX_DELAY,
        )
        jitter = backoff * (0.8 + 0.4 * _random.random())  # ±20%
        log.debug(
            "agent: reconnect attempt %d in %.1fs (capped %.0fs)",
            consecutive_connect_failures, jitter, _RECONNECT_MAX_DELAY,
        )
        # Wait for the reconnect delay but wake immediately if shutdown fires.
        shutdown_event.wait(timeout=jitter)

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
    ap.add_argument(
        "--no-gui",
        action="store_true",
        help="CLI-only mode (skip the desktop window). Default behavior "
             "is to launch the GUI; use this for headless servers or "
             "scripted automation.",
    )
    args = ap.parse_args()

    # v0.6.6: the PyInstaller spec sets console=False so the GUI launches
    # without a background terminal window. For --no-gui mode on Windows,
    # try to re-attach to the parent console (cmd.exe / PowerShell) so
    # users running the .exe from a terminal still see stdout. Best-
    # effort — if launched from Explorer with --no-gui, output goes
    # to NUL but agent.log + boot.log still capture everything.
    if args.no_gui and sys.platform == "win32":
        try:
            import ctypes
            ATTACH_PARENT_PROCESS = -1
            if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
                # Re-open the std streams so print() and logging's
                # StreamHandler actually write to the attached console.
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        except Exception:
            # No parent console (launched from Explorer) — silent fallback.
            pass

    log_path = configure_logging(log_dir=args.log_dir, verbose=args.verbose)
    print(f"Logs: {log_path}")
    print(f"Boot trace: {_boot_log_path()}")
    _boot_write(f"main: configure_logging ok, log_dir={log_path}")

    _install_signal_handlers()

    if args.no_gui:
        run(args.server)
        return

    # GUI mode: window on the main thread, network loop on a daemon
    # thread that updates the shared AgentState.
    try:
        from agent.gui import AgentGUI, StateLogHandler
    except Exception:
        log.exception(
            "GUI failed to import — falling back to CLI mode. "
            "Pass --no-gui to suppress this message."
        )
        run(args.server)
        return

    global _state
    _state = _AgentState(server_url=args.server, version=__version__)

    # Mirror log records into the GUI's activity-log box.
    handler = StateLogHandler(_state)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s",
                                           datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(handler)

    gui = AgentGUI(_state, shutdown_event=_shutdown_event)

    def _network_thread():
        try:
            run(args.server)
        except SystemExit as e:
            # Pairing cancelled / explicit shutdown — surface to the GUI log.
            if _state is not None:
                _state.append_log(str(e) or "Stopped.")
        except Exception:
            log.exception("Network loop crashed")
        finally:
            _shutdown_event.set()

    t = threading.Thread(target=_network_thread, daemon=True,
                         name="agent-network")
    t.start()

    gui.run()
    # Window closed → make sure the network thread sees the shutdown.
    _shutdown_event.set()
    _state.provide_pairing_code(None)


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
