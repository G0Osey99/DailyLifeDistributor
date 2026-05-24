"""Print every paired agent device, with online status from the relay.

When to use:
    The user reports "my agent disappeared" or you're cleaning up
    revoked devices. Online column reflects the *current process*'s
    relay state; inside the container it's the live picture.

Usage:
    docker exec dld python scripts/list_agents.py --help
"""
from __future__ import annotations

import argparse
import os
import sys

# Repo root on sys.path so `from core import ...` works when invoked directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(
        prog="list_agents",
        description="Paired-device table + online status.",
    )
    parser.add_argument("--show-revoked", action="store_true",
                        help="Include revoked devices (hidden by default)")
    args = parser.parse_args()

    from core import db as _db
    _db.init_db()

    # Online set: lookup the singleton relay if one's been registered in
    # this process. Outside the running app the set is always empty.
    online_ids: set[str] = set()
    try:
        from core import relay as _relay
        if _relay._default_relay is not None:  # type: ignore[attr-defined]
            for entry in _relay._default_relay.online_agents(  # type: ignore[attr-defined]
                _relay._default_account  # type: ignore[attr-defined]
            ):
                online_ids.add(entry.get("device_id", ""))
    except Exception:
        pass

    with _db._get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, hostname, hwid_hash, last_seen_at, revoked "
            "FROM agent_devices ORDER BY last_seen_at DESC NULLS LAST, id"
        ).fetchall()

    if not rows:
        print("No agent devices registered.")
        return

    if not args.show_revoked:
        rows = [r for r in rows if not r["revoked"]]
        if not rows:
            print("No active (non-revoked) agent devices. Use --show-revoked to include them.")
            return

    headers = ("id", "name", "hostname", "hwid", "last_seen", "revoked", "online")
    print(f"{'id':<10}  {'name':<20}  {'hostname':<24}  {'hwid':<10}  "
          f"{'last_seen':<25}  {'revoked':<7}  {'online':<6}")
    print("-" * 110)
    for r in rows:
        dev_id = (r["id"] or "")[:8]
        name = (r["name"] or "-")[:20]
        host = (r["hostname"] or "-")[:24]
        hwid_short = (r["hwid_hash"] or "-")[:8]
        last_seen = (r["last_seen_at"] or "-")[:25]
        revoked = "yes" if r["revoked"] else "no"
        online = "yes" if (r["id"] in online_ids) else "no"
        print(f"{dev_id:<10}  {name:<20}  {host:<24}  {hwid_short:<10}  "
              f"{last_seen:<25}  {revoked:<7}  {online:<6}")


if __name__ == "__main__":
    main()
