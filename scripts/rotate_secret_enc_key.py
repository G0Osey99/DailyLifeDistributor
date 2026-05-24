"""Rotate the master Fernet key (SECRET_ENC_KEY).

Re-encrypts every row of the ``secrets`` table and every
``users.totp_secret_encrypted`` column in a single transaction.

When to use:
    Quarterly rotation, post-incident (suspected key exposure), or when
    an operator with the old key leaves. **Back up state.db first** and
    keep the old key around until verification succeeds — losing the key
    means losing every encrypted blob with no recovery path.

Usage:
    python scripts/rotate_secret_enc_key.py --help

    # Dry-run (default): prints how many rows would change, writes nothing
    python scripts/rotate_secret_enc_key.py --old <old-key> --new <new-key>

    # Or read the old key from an env var (avoids leaving it in shell history)
    OLD_SECRET_ENC_KEY=<old> python scripts/rotate_secret_enc_key.py \\
        --from-env OLD_SECRET_ENC_KEY --new <new-key>

    # Actually write
    python scripts/rotate_secret_enc_key.py --old <old> --new <new> --apply
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

# Repo root on sys.path so `from core import ...` works when invoked directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet, InvalidToken


def _resolve_old_key(args) -> str:
    if args.from_env:
        v = (os.environ.get(args.from_env) or "").strip()
        if not v:
            print(f"ERROR: --from-env {args.from_env} but ${args.from_env} is empty",
                  file=sys.stderr)
            sys.exit(2)
        return v
    if args.old:
        return args.old.strip()
    print("ERROR: must provide --old <key> or --from-env <ENV_VAR>", file=sys.stderr)
    sys.exit(2)


def _check_db_writable(db_path: str) -> None:
    """Best-effort 'is the DB locked' check.

    SQLite gives 'database is locked' as a runtime error when we try to
    BEGIN IMMEDIATE while another connection holds a write lock. We pre-check
    so the rotation aborts cleanly rather than half-way through the loop.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            print(f"ERROR: {db_path} is currently held by another process "
                  "(probably the running app). Stop the container/process "
                  "before rotating.", file=sys.stderr)
            sys.exit(3)
        raise


def main():
    parser = argparse.ArgumentParser(
        prog="rotate_secret_enc_key",
        description="Re-encrypt every secret blob with a new Fernet key.",
    )
    parser.add_argument("--old", help="Old SECRET_ENC_KEY value (urlsafe-b64)")
    parser.add_argument("--from-env", metavar="VAR",
                        help="Read the old key from this env var instead of --old")
    parser.add_argument("--new", required=True,
                        help="New SECRET_ENC_KEY value (urlsafe-b64)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write changes (default is dry-run)")
    args = parser.parse_args()

    old_key = _resolve_old_key(args)
    new_key = args.new.strip()

    try:
        old_f = Fernet(old_key.encode("ascii"))
        new_f = Fernet(new_key.encode("ascii"))
    except (ValueError, TypeError) as e:
        print(f"ERROR: invalid Fernet key: {e}", file=sys.stderr)
        sys.exit(2)

    # Briefly install the OLD key as SECRET_ENC_KEY so core.db.init_db()
    # doesn't fail on its own master-key validation when imported.
    os.environ["SECRET_ENC_KEY"] = old_key
    from core import db as _db

    db_path = _db._DB_PATH
    print(f"DB: {db_path}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    _check_db_writable(db_path)

    secrets_done = 0
    secrets_failed = 0
    totp_done = 0
    totp_failed = 0
    started = time.time()

    with _db._get_conn() as conn:
        if args.apply:
            conn.execute("BEGIN IMMEDIATE")
        # --- secrets ---
        rows = conn.execute(
            "SELECT name, value FROM secrets"
        ).fetchall()
        for row in rows:
            name = row["name"]
            ciphertext = bytes(row["value"])
            try:
                plaintext = old_f.decrypt(ciphertext)
            except InvalidToken:
                secrets_failed += 1
                print(f"  WARN: secret {name!r} could not be decrypted with old key — skipping",
                      file=sys.stderr)
                continue
            new_ct = new_f.encrypt(plaintext)
            if args.apply:
                conn.execute("UPDATE secrets SET value=? WHERE name=?",
                             (new_ct, name))
            secrets_done += 1

        # --- users.totp_secret_encrypted ---
        try:
            users = conn.execute(
                "SELECT id, totp_secret_encrypted FROM users "
                "WHERE totp_secret_encrypted IS NOT NULL "
                "  AND totp_secret_encrypted != ''"
            ).fetchall()
        except sqlite3.OperationalError:
            # `users` table absent (e.g. a very old DB) — nothing to do.
            users = []

        for row in users:
            uid = row["id"]
            ct = row["totp_secret_encrypted"]
            if isinstance(ct, str):
                ct_bytes = ct.encode("ascii")
            else:
                ct_bytes = bytes(ct)
            try:
                plaintext = old_f.decrypt(ct_bytes)
            except InvalidToken:
                totp_failed += 1
                print(f"  WARN: users.totp_secret_encrypted for user_id={uid} "
                      "could not be decrypted with old key — skipping", file=sys.stderr)
                continue
            new_ct = new_f.encrypt(plaintext).decode("ascii")
            if args.apply:
                conn.execute(
                    "UPDATE users SET totp_secret_encrypted=? WHERE id=?",
                    (new_ct, uid),
                )
            totp_done += 1

        if args.apply:
            conn.commit()

    elapsed = time.time() - started
    print()
    print("Summary:")
    print(f"  secrets re-encrypted: {secrets_done}  (failed: {secrets_failed})")
    print(f"  TOTP rows re-encrypted: {totp_done}  (failed: {totp_failed})")
    print(f"  elapsed: {elapsed:.2f}s")
    if not args.apply:
        print()
        print("DRY-RUN: nothing was written. Re-run with --apply when ready.")
    else:
        print()
        print("DONE. Update .env with the new SECRET_ENC_KEY and restart the app.")

    if secrets_failed or totp_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
