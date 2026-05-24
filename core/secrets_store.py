"""Encrypted secret store backed by the `secrets` table in state.db.

Values are Fernet-encrypted (core.crypto) before they touch disk. Two kinds:
  - 'kv'   : UTF-8 string secrets (API keys, password hashes)
  - 'blob' : arbitrary bytes (OAuth token JSON, Playwright storage_state)

`materialize_blob_to_tempfile` decrypts a blob to a 0600 temp file for the
brief window a third-party library needs a real file path, then deletes it.

Multi-tenant phase β: every accessor takes an optional ``org_id`` kwarg.
When ``org_id`` is provided, the secret name is namespaced under
``org:<id>:<name>`` so two orgs can hold a ``yt_token`` of their own
without colliding. When ``org_id`` is None, behaviour is unchanged from
phase α (legacy single-tenant rows).

The schema's ``org_id`` column (added in phase α) is populated to match the
``org_id`` arg, so a future migration that switches the primary key to
``(name, org_id)`` can dedupe legacy rows without re-decrypting them.
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


def _scoped(name: str, org_id: int | None) -> str:
    """Return the storage name for *name* in scope *org_id*.

    ``org_id`` is None  → legacy unscoped (single-tenant) name.
    ``org_id`` is int   → ``org:<id>:<name>``. Prefix is reserved.

    Raises ValueError if *name* already starts with a reserved prefix
    (``platform:`` or ``org:``) — defense-in-depth so callers can't
    accidentally cross tenant/platform scope.
    """
    if name.startswith("platform:") or name.startswith("org:"):
        raise ValueError(
            f"secret name {name!r} uses a reserved prefix; "
            "use set_platform_secret or pass org_id, not both."
        )
    if org_id is None:
        return name
    return f"org:{int(org_id)}:{name}"


def _write(storage_name: str, kind: str, raw: bytes, *, org_id: int | None = None) -> None:
    """Write an encrypted value directly by its literal storage name."""
    # Invariant: a platform:* storage name must NEVER carry a non-None org_id.
    # The two namespaces are intentionally orthogonal; a row with both prefixed
    # name and org_id set would confuse the future (name, org_id) PK migration.
    assert (not storage_name.startswith("platform:")) or org_id is None, (
        f"_write({storage_name!r}, org_id={org_id!r}): platform names must "
        "have org_id=None"
    )
    token = crypto.encrypt(raw)
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO secrets (name, kind, value, updated_at, org_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, "
            "value=excluded.value, updated_at=excluded.updated_at, "
            "org_id=excluded.org_id",
            (storage_name, kind, token, _now(), org_id),
        )
        conn.commit()


def _read(storage_name: str) -> bytes | None:
    """Read and decrypt by literal storage name; None if missing/corrupt."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM secrets WHERE name=?", (storage_name,),
        ).fetchone()
    if row is None:
        return None
    try:
        return crypto.decrypt(bytes(row["value"]))
    except crypto.DecryptError:
        log.error(
            "Secret %r could not be decrypted; treating as unset.",
            storage_name,
        )
        return None


def _set(name: str, kind: str, raw: bytes, *, org_id: int | None = None) -> None:
    _write(_scoped(name, org_id), kind, raw, org_id=org_id)


def _get_raw(name: str, *, org_id: int | None = None) -> bytes | None:
    return _read(_scoped(name, org_id))


def set_secret(name: str, plaintext: str, *, org_id: int | None = None) -> None:
    _set(name, "kv", plaintext.encode("utf-8"), org_id=org_id)


def get_secret(name: str, *, org_id: int | None = None) -> str | None:
    raw = _get_raw(name, org_id=org_id)
    return None if raw is None else raw.decode("utf-8")


def set_blob(name: str, data: bytes, *, org_id: int | None = None) -> None:
    _set(name, "blob", data, org_id=org_id)


def get_blob(name: str, *, org_id: int | None = None) -> bytes | None:
    return _get_raw(name, org_id=org_id)


def has_secret(name: str, *, org_id: int | None = None) -> bool:
    """Return True if a row exists for *name* in scope *org_id*.

    Note: this returns True even if the stored value is currently
    undecryptable (wrong/rotated key); callers must still handle a None
    result from get_secret/get_blob.
    """
    storage_name = _scoped(name, org_id)
    with _get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM secrets WHERE name=?", (storage_name,),
        ).fetchone() is not None


def delete_secret(name: str, *, org_id: int | None = None) -> None:
    storage_name = _scoped(name, org_id)
    with _get_conn() as conn:
        conn.execute("DELETE FROM secrets WHERE name=?", (storage_name,))
        conn.commit()


# ---------------------------------------------------------------------------
# Platform-scoped namespace
#
# Secrets that are shared across every tenant (the GCP OAuth client used by
# all orgs for YouTube authentication is the canonical example) live under
# the ``platform:<name>`` storage prefix. They are NOT visible from the
# per-org accessors above — reads MUST come through these wrappers, which
# guarantees no caller accidentally lands a tenant secret in platform scope
# (or vice versa).
# ---------------------------------------------------------------------------

_PLATFORM_PREFIX = "platform:"


def list_secret_names(*, org_id: int | None = None) -> list[str]:
    """List secret names visible in scope *org_id*.

    ``org_id`` is None  → only legacy (unscoped) names, prefix-stripped of
                          any accidental ``org:N:`` or ``platform:`` rows.
    ``org_id`` is int   → only secrets stored under that scope, returned
                          with the scope prefix stripped.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM secrets ORDER BY name"
        ).fetchall()
    if org_id is None:
        return [
            r["name"] for r in rows
            if not r["name"].startswith("org:")
            and not r["name"].startswith(_PLATFORM_PREFIX)
        ]
    prefix = f"org:{int(org_id)}:"
    return [
        r["name"][len(prefix):] for r in rows
        if r["name"].startswith(prefix)
    ]


def _platform_scoped(name: str) -> str:
    return f"{_PLATFORM_PREFIX}{name}"


def set_platform_secret(name: str, plaintext: str) -> None:
    _write(_platform_scoped(name), "kv", plaintext.encode("utf-8"), org_id=None)


def get_platform_secret(name: str) -> str | None:
    raw = _read(_platform_scoped(name))
    return None if raw is None else raw.decode("utf-8")


def set_platform_blob(name: str, data: bytes) -> None:
    _write(_platform_scoped(name), "blob", data, org_id=None)


def get_platform_blob(name: str) -> bytes | None:
    return _read(_platform_scoped(name))


def has_platform_secret(name: str) -> bool:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM secrets WHERE name=?", (_platform_scoped(name),),
        ).fetchone() is not None


def delete_platform_secret(name: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM secrets WHERE name=?", (_platform_scoped(name),),
        )
        conn.commit()


@contextmanager
def materialize_blob_to_tempfile(
    name: str, suffix: str = "", *, org_id: int | None = None,
):
    """Decrypt a blob secret to a temp file; delete it on exit.

    The temp file is created 0600 on POSIX (Windows ignores POSIX modes).
    Yields the temp-file path, or None if the secret is unset.
    """
    data = get_blob(name, org_id=org_id)
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
