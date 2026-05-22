"""Idempotently import existing plaintext secrets into the encrypted store.

Imports:
  - API keys from env: UNSPLASH_ACCESS_KEY, PEXELS_API_KEY
  - YouTube: client_secrets.json (blob), token.json (kv string)
  - Playwright: *_session.json (blob, one per service)

Safe to run repeatedly: a secret already present in the store is left alone.
Run manually with `python -m scripts.migrate_secrets`, or let it run
automatically on app boot.
"""
from __future__ import annotations

import logging
import os

from core import secrets_store
from core.config import PROJECT_ROOT

log = logging.getLogger(__name__)

_ENV_KEYS = ("UNSPLASH_ACCESS_KEY", "PEXELS_API_KEY")
_SESSION_FILES = (
    "simplecast_session.json",
    "vista_social_session.json",
    "rock_session.json",
)


def _import_kv_from_env(name: str) -> bool:
    if secrets_store.has_secret(name):
        return False
    val = (os.environ.get(name) or "").strip()
    if not val:
        return False
    secrets_store.set_secret(name, val)
    return True


def _shred(path: str) -> None:
    """Best-effort secure-ish delete of a plaintext secret file.

    Overwrite the contents before unlinking so the bytes aren't trivially
    recoverable from the same inode, then remove it. Errors are swallowed —
    failing to delete must never block boot, but we try hard because leaving
    a plaintext credential (a YouTube refresh token, OAuth client secret, or
    browser session cookies) next to the app defeats the encrypted store.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "r+b", buffering=0) as f:
            f.write(b"\x00" * size)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        os.remove(path)
    except OSError:
        pass


def _ensure_blob_and_shred(name: str, path: str) -> bool:
    """Import a file-backed blob secret if missing, then shred the plaintext.

    Shreds whenever the store holds the secret and a plaintext copy lingers —
    including idempotent re-runs where the import itself is skipped — so a
    leftover plaintext credential never survives a boot.
    """
    imported = False
    if not secrets_store.has_secret(name) and os.path.exists(path):
        with open(path, "rb") as f:
            secrets_store.set_blob(name, f.read())
        imported = True
    if secrets_store.has_secret(name) and os.path.exists(path):
        _shred(path)
    return imported


def _ensure_kv_and_shred(name: str, path: str) -> bool:
    imported = False
    if not secrets_store.has_secret(name) and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            secrets_store.set_secret(name, f.read())
        imported = True
    if secrets_store.has_secret(name) and os.path.exists(path):
        _shred(path)
    return imported


def run() -> list[str]:
    """Import any missing plaintext secrets. Returns names imported."""
    imported: list[str] = []

    for key in _ENV_KEYS:
        if _import_kv_from_env(key):
            imported.append(key)

    if _ensure_blob_and_shred(
        "youtube.client_secrets", os.path.join(PROJECT_ROOT, "client_secrets.json")
    ):
        imported.append("youtube.client_secrets")
    if _ensure_kv_and_shred(
        "youtube.token", os.path.join(PROJECT_ROOT, "token.json")
    ):
        imported.append("youtube.token")

    for fname in _SESSION_FILES:
        base = os.path.splitext(fname)[0]
        if _ensure_blob_and_shred(
            f"playwright.{base}", os.path.join(PROJECT_ROOT, fname)
        ):
            imported.append(f"playwright.{base}")

    if imported:
        log.info("Imported %d plaintext secret(s) into the store: %s",
                 len(imported), ", ".join(imported))
    return imported


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    names = run()
    print(f"Imported {len(names)} secret(s): {', '.join(names) or '(none)'}")
