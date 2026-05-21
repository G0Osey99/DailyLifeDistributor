"""Encrypted secret store backed by the `secrets` table in state.db.

Values are Fernet-encrypted (core.crypto) before they touch disk. Two kinds:
  - 'kv'   : UTF-8 string secrets (API keys, password hashes)
  - 'blob' : arbitrary bytes (OAuth token JSON, Playwright storage_state)

`materialize_blob_to_tempfile` decrypts a blob to a 0600 temp file for the
brief window a third-party library needs a real file path, then deletes it.
"""
from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from core import crypto
from core.db import _get_conn

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set(name: str, kind: str, raw: bytes) -> None:
    token = crypto.encrypt(raw)
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO secrets (name, kind, value, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, "
            "value=excluded.value, updated_at=excluded.updated_at",
            (name, kind, token, _now()),
        )
        conn.commit()


def _get_raw(name: str) -> bytes | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM secrets WHERE name=?", (name,)
        ).fetchone()
    if row is None:
        return None
    try:
        return crypto.decrypt(bytes(row["value"]))
    except crypto.DecryptError:
        log.error("Secret %r could not be decrypted; treating as unset.", name)
        return None


def set_secret(name: str, plaintext: str) -> None:
    _set(name, "kv", plaintext.encode("utf-8"))


def get_secret(name: str) -> str | None:
    raw = _get_raw(name)
    return None if raw is None else raw.decode("utf-8")


def set_blob(name: str, data: bytes) -> None:
    _set(name, "blob", data)


def get_blob(name: str) -> bytes | None:
    return _get_raw(name)


def has_secret(name: str) -> bool:
    """Return True if a row exists for `name`.

    Note: this returns True even if the stored value is currently
    undecryptable (wrong/rotated key); callers must still handle a None
    result from get_secret/get_blob.
    """
    with _get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM secrets WHERE name=?", (name,)
        ).fetchone() is not None


def delete_secret(name: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM secrets WHERE name=?", (name,))
        conn.commit()


def list_secret_names() -> list[str]:
    with _get_conn() as conn:
        return [r["name"] for r in conn.execute(
            "SELECT name FROM secrets ORDER BY name"
        ).fetchall()]


@contextmanager
def materialize_blob_to_tempfile(name: str, suffix: str = ""):
    """Decrypt a blob secret to a temp file; delete it on exit.

    The temp file is created 0600 on POSIX (Windows ignores POSIX modes).
    Yields the temp-file path, or None if the secret is unset.
    """
    data = get_blob(name)
    if data is None:
        yield None
        return
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)  # POSIX only; Windows ignores POSIX modes
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
