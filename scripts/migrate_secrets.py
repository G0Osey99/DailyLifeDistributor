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


def _import_blob_from_file(name: str, path: str) -> bool:
    if secrets_store.has_secret(name):
        return False
    if not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        secrets_store.set_blob(name, f.read())
    return True


def _import_kv_from_file(name: str, path: str) -> bool:
    if secrets_store.has_secret(name):
        return False
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        secrets_store.set_secret(name, f.read())
    return True


def run() -> list[str]:
    """Import any missing plaintext secrets. Returns names imported."""
    imported: list[str] = []

    for key in _ENV_KEYS:
        if _import_kv_from_env(key):
            imported.append(key)

    if _import_blob_from_file(
        "youtube.client_secrets", os.path.join(PROJECT_ROOT, "client_secrets.json")
    ):
        imported.append("youtube.client_secrets")
    if _import_kv_from_file(
        "youtube.token", os.path.join(PROJECT_ROOT, "token.json")
    ):
        imported.append("youtube.token")

    for fname in _SESSION_FILES:
        base = os.path.splitext(fname)[0]
        if _import_blob_from_file(
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
