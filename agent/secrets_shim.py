"""Drop-in replacement for `core.secrets_store` on the agent.

Installed into sys.modules as 'core.secrets_store' at agent startup so
bundled uploaders (notably uploaders/youtube_uploader.py and the Playwright-
based uploaders that pull session JSON through core.playwright_session) work
unchanged.

Backed by an in-memory dict + per-call tempfiles; mutations are emitted
back to the server as `credentials_updated` events. The server is the
source of truth — the shim never touches a SQLite DB or master key.

Surface (mirrors core.secrets_store):
  - ``get_secret(key) -> str | None``
  - ``set_secret(key, value: str) -> None``  (emits ``credentials_updated``)
  - ``delete_secret(key) -> None``           (emits ``credentials_updated``)
  - ``get_blob(key) -> bytes | None``        (UTF-8 encoded view of stored str)
  - ``set_blob(key, data: bytes) -> None``   (decoded UTF-8; emits credentials_updated)
  - ``has_secret(key) -> bool``
  - ``materialize_blob_to_tempfile(key, *, suffix='') -> Iterator[str | None]``

Encoding assumption: the agent receives credentials as JSON strings in the
job-plan envelope, so the in-memory store is keyed ``str -> str``. The blob
methods are a thin UTF-8 adapter so PlaywrightSession's ``get_blob`` /
``set_blob`` (which trade in ``bytes``) keep working unchanged. Non-UTF-8
blobs are not supported (logged + dropped on set).
"""
from __future__ import annotations
import contextlib
import logging
import os
import tempfile
from typing import Callable, Optional, Iterator

_EmitFn = Callable[[dict], None]
_log = logging.getLogger(__name__)


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

    def has_secret(self, key: str) -> bool:
        """True if the shim is currently holding a value for ``key``."""
        return key in self._d

    def get_blob(self, key: str) -> Optional[bytes]:
        """Return the stored value as UTF-8 bytes, or None if unset.

        PlaywrightSession passes Playwright storage_state JSON through
        get_blob/set_blob. We keep the in-memory dict as ``str -> str``
        and transparently encode on the way out.
        """
        val = self._d.get(key)
        if val is None:
            return None
        return val.encode("utf-8")

    def set_blob(self, key: str, data: bytes) -> None:
        """Store *data* (UTF-8 bytes) and emit ``credentials_updated``.

        Non-UTF-8 payloads are logged and dropped — the server treats all
        managed credentials as text. (We have no binary blobs in flight
        today; if that changes, switch the store to ``bytes`` and base64
        the wire format.)
        """
        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            _log.warning(
                "secrets_shim.set_blob: dropping non-UTF-8 payload for %r (%s); "
                "the agent only forwards text credentials to the server.",
                key, exc,
            )
            return
        # Reuse set_secret so the credentials_updated event fires uniformly.
        self.set_secret(key, decoded)

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
    mod.get_secret = shim.get_secret                       # type: ignore[attr-defined]
    mod.set_secret = shim.set_secret                       # type: ignore[attr-defined]
    mod.delete_secret = shim.delete_secret                 # type: ignore[attr-defined]
    mod.has_secret = shim.has_secret                       # type: ignore[attr-defined]
    mod.get_blob = shim.get_blob                           # type: ignore[attr-defined]
    mod.set_blob = shim.set_blob                           # type: ignore[attr-defined]
    mod.materialize_blob_to_tempfile = shim.materialize_blob_to_tempfile  # type: ignore[attr-defined]
    _sys.modules["core.secrets_store"] = mod
    return shim
