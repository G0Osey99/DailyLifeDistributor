"""Phase 1 agent entrypoint: pair if needed, connect, reply pong to ping.

Run:  python -m agent.main --server https://autoalert.pro
First run prompts for a pairing code (generated in the web UI).
"""
from __future__ import annotations

import argparse
import socket
import time

from agent import config, pair
from agent.transport import AgentConnection


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
    if msg.get("type") == "ping":
        conn.send({"v": 1, "type": "pong", "payload": msg.get("payload", {})})


def run(server_url: str) -> None:
    token = _ensure_paired(server_url)
    while True:
        conn = AgentConnection(server_url, token)
        try:
            conn.connect()
            while conn.run_once(lambda m: _on_message(conn, m)):
                pass
        except Exception:  # noqa: BLE001 — reconnect on any drop
            pass
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
