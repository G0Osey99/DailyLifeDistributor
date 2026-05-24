"""Print the program-owner user(s) and every org/role they have.

When to use:
    Onboarding a new instance, or after rotation of the program-owner
    account. Quick sanity check that exactly one program owner exists
    and they have the org memberships they should.

Usage:
    docker exec dld python scripts/show_program_owner.py --help
"""
from __future__ import annotations

import argparse
import os
import sys

# Repo root on sys.path so `from core import ...` works when invoked directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(
        prog="show_program_owner",
        description="Dump program-owner users and their org memberships.",
    )
    parser.parse_args()

    from core import db as _db
    _db.init_db()

    with _db._get_conn() as conn:
        owners = conn.execute(
            "SELECT id, username, email, created_at "
            "FROM users WHERE program_owner = 1 "
            "ORDER BY id"
        ).fetchall()

        if not owners:
            print("No program-owner users exist in this database.")
            print()
            print("Bootstrap one by setting PROGRAM_OWNER_EMAIL + "
                  "INITIAL_ADMIN_PASSWORD and restarting the app — "
                  "`core.migration_bootstrap.run_migration()` will create it.")
            sys.exit(1)

        for owner in owners:
            uid = owner["id"]
            print(f"Program owner: id={uid}  username={owner['username']!r}  "
                  f"email={owner['email']!r}  created_at={owner['created_at']}")
            mems = conn.execute(
                "SELECT m.org_id, m.role, m.joined_at, o.name "
                "FROM org_memberships m LEFT JOIN organizations o ON o.id = m.org_id "
                "WHERE m.user_id = ? ORDER BY m.org_id",
                (uid,),
            ).fetchall()
            if not mems:
                print("  (no org memberships — program owner can still admin "
                      "globally, but won't see per-org settings until added)")
            else:
                print(f"  {'org_id':<7}  {'role':<10}  {'joined_at':<25}  org_name")
                print(f"  {'-' * 7}  {'-' * 10}  {'-' * 25}  {'-' * 24}")
                for m in mems:
                    nm = (m["name"] or f"<deleted org {m['org_id']}>")[:40]
                    print(f"  {m['org_id']:<7}  {m['role']:<10}  "
                          f"{(m['joined_at'] or '-'):<25}  {nm}")
            print()


if __name__ == "__main__":
    main()
