"""Phase 1 agent entrypoint: pair if needed, connect, reply pong to ping.

Run:  python -m agent.main --server https://autoalert.pro
First run prompts for a pairing code (generated in the web UI).
"""
from __future__ import annotations

import argparse
import logging
import socket
import time

from agent import config, pair, scan, updater
from agent.transport import AgentConnection

log = logging.getLogger(__name__)


def _device_name() -> str:
    return socket.gethostname() or "device"


def _ensure_paired(server_url: str) -> str:
    token = config.get_token()
    if token:
        return token
    code = input("Enter pairing code from the website: ").strip()
    if not pair.redeem(server_url, code, _device_name()):
        raise SystemExit("Pairing failed — check the code and try again.")
    return config.get_token()


def _on_message(conn: AgentConnection, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "ping":
        conn.send({"v": 1, "type": "pong", "payload": msg.get("payload", {})})
    elif mtype == "scan_request":
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


def run(server_url: str) -> None:
    token = _ensure_paired(server_url)
    try:
        updater.check_and_apply(server_url)
    except Exception:
        log.debug("update check raised; continuing", exc_info=True)
    while True:
        conn = AgentConnection(server_url, token)
        try:
            conn.connect()
            # Bind conn into the callback's default arg so the closure can't
            # accidentally pick up a later iteration's connection.
            while conn.run_once(lambda m, c=conn: _on_message(c, m)):
                pass
        except Exception:  # noqa: BLE001 — reconnect on any drop
            log.debug("agent connection dropped; reconnecting", exc_info=True)
        finally:
            conn.close()
        time.sleep(3)  # backoff before reconnect


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=config.get_server_url() or "https://autoalert.pro")
    args = ap.parse_args()
    run(args.server)


if __name__ == "__main__":
    main()
