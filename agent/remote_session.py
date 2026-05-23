"""Context-manager wrapper that mimics core.playwright_session.PlaywrightSession
on the agent. On enter: writes the named credential to a tempfile and yields
the path. On exit: hashes the file; if it changed, write back through the
shim (which emits credentials_updated)."""
from __future__ import annotations
import hashlib
import os
import tempfile
from typing import Optional

from agent.secrets_shim import Shim


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class RemotePlaywrightSession:
    def __init__(self, shim: Shim, key: str, *, suffix: str = ".json") -> None:
        self._shim = shim
        self._key = key
        self._suffix = suffix
        self._path: Optional[str] = None
        self._original_hash: Optional[str] = None

    def __enter__(self) -> str:
        val = self._shim.get_secret(self._key) or ""
        fd, path = tempfile.mkstemp(suffix=self._suffix, prefix="dld-sess-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(val)
        self._path = path
        self._original_hash = _sha(val.encode("utf-8"))
        return path

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._path is not None
        try:
            with open(self._path, "rb") as f:
                new_bytes = f.read()
        except FileNotFoundError:
            new_bytes = b""
        new_hash = _sha(new_bytes)
        if new_hash != self._original_hash:
            self._shim.set_secret(self._key, new_bytes.decode("utf-8"))
        try:
            os.remove(self._path)
        except OSError:
            pass
        self._path = None
