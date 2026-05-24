"""Print today's YouTube Data API quota — global counter + per-org rows.

When to use:
    A run reports "quota exceeded" before the documented cap. Read here
    to see actual consumption: which org burned the most, how close to
    the cap, whether the global counter has drifted.

Usage:
    docker exec dld python scripts/quota_status.py --help
"""
from __future__ import annotations

import argparse
import os
import sys

# Repo root on sys.path so `from core import ...` works when invoked directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bar(pct: float, width: int = 30) -> str:
    filled = max(0, min(width, int(round(width * pct / 100.0))))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def main():
    parser = argparse.ArgumentParser(
        prog="quota_status",
        description="Today's YouTube quota usage, global + per-org.",
    )
    parser.parse_args()

    from core import db as _db
    from core import quota as _quota
    _db.init_db()

    cap = int(_quota.DAILY_QUOTA)
    today = _quota._today_key()
    print(f"Today (America/Los_Angeles): {today}")
    print(f"Daily cap: {cap:,} units\n")

    # ---- global counter ----
    used = int(_quota.get_quota_used() or 0)
    pct = 100.0 * used / max(1, cap)
    print(f"Global  {used:>8,} / {cap:>8,}  {pct:5.1f}%  {_bar(pct)}")

    # ---- per-org rows ----
    with _db._get_conn() as conn:
        rows = conn.execute(
            "SELECT yt.org_id, yt.units_used, o.name "
            "FROM yt_quota_usage yt LEFT JOIN organizations o ON o.id = yt.org_id "
            "WHERE yt.quota_date = ? "
            "ORDER BY yt.units_used DESC",
            (today,),
        ).fetchall()

    if not rows:
        print("\nNo per-org rows for today.")
        return

    print(f"\n{'org_id':<7}  {'name':<24}  {'used':>10}  {'% cap':>6}  bar")
    print("-" * 80)
    for r in rows:
        used = int(r["units_used"])
        name = (r["name"] or f"<deleted org {r['org_id']}>")[:24]
        pct = 100.0 * used / max(1, cap)
        print(f"{r['org_id']:<7}  {name:<24}  {used:>10,}  {pct:5.1f}%  {_bar(pct, width=20)}")


if __name__ == "__main__":
    main()
