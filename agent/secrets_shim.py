"""Drop-in replacement for `core.secrets_store` on the agent.

Installed into sys.modules as 'core.secrets_store' at agent startup so
bundled uploaders (notably uploaders/youtube_uploader.py) work unchanged.
Backed by an in-memory dict + per-call tempfiles; mutations are emitted
back to the server as `credentials_updated` events. The server is the
source of truth — the shim never touches a SQLite DB or master key.
"""
from __future__ import annotations
import contextlib
import os
import tempfile
from typing import Callable, Optional, Iterator

_EmitFn = Callable[[dict], None]


class Shim:
    def __init__(self, *, initial: Optional[dict] = None,
                 emit: Optional[_EmitFn] = None) -> None:
        self._d: dict[str, str] = dict(initial or {})
        self._emit: _EmitFn = emit or (lambda _frame: None)

    def get_secret(self, key: str) -> Optional[str]:
        return self._d.get(key)

    def set_secret(self, key: str, value: str) -> None:
        self._d[key] = value
        self._emit({"type": "credentials_updated", "key": key, "value": value})

    def delete_secret(self, key: str) -> None:
        self._d.pop(key, None)
        self._emit({"type": "credentials_updated", "key": key, "value": ""})

    @contextlib.contextmanager
    def materialize_blob_to_tempfile(self, key: str, *,
                                     suffix: str = "") -> Iterator[Optional[str]]:
        val = self._d.get(key)
        if val is None:
            yield None
            return
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="dld-cred-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(val)
            yield path
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


# Default singleton used when the shim is installed module-level.
_default = Shim()


def install_as_core_secrets_store(*, initial: dict, emit: _EmitFn) -> Shim:
    """Replace core.secrets_store in sys.modules with module-level functions
    that delegate to a fresh Shim. Returns the new Shim so the dispatch
    layer can keep a handle for swapping `initial` between jobs."""
    import sys as _sys, types as _types
    shim = Shim(initial=initial, emit=emit)
    mod = _types.ModuleType("core.secrets_store")
    mod.get_secret = shim.get_secret           # type: ignore[attr-defined]
    mod.set_secret = shim.set_secret           # type: ignore[attr-defined]
    mod.delete_secret = shim.delete_secret     # type: ignore[attr-defined]
    mod.materialize_blob_to_tempfile = shim.materialize_blob_to_tempfile  # type: ignore[attr-defined]
    _sys.modules["core.secrets_store"] = mod
    return shim
