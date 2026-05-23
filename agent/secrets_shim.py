"""Drop-in replacement for `core.secrets_store` on the agent.

Installed into sys.modules as 'core.secrets_store' at agent startup so
bundled uploaders (notably uploaders/youtube_uploader.py and the Playwright-
based uploaders that pull session JSON through core.playwright_session) work
unchanged.

Backed by an in-memory dict + per-call tempfiles; mutations are emitted
back to the server as `credentials_updated` events. The server is the
source of truth — the shim never touches a SQLite DB or master key.

Encryption-at-rest (Phase 3 hardening)
---------------------------------------
Credentials are stored Fernet-encrypted in the in-memory dict and only
decrypted at use time inside ``get_secret`` / ``get_blob`` /
``materialize_blob_to_tempfile``. The Fernet key is generated per-process
on first use and **never persisted** — restarting the agent (or a new
``install_as_core_secrets_store`` call building a fresh Shim) regenerates
the key, so old ciphertexts are dead.

This narrows the memory-dump / debugger-attach window: a casual core dump
won't surface the plaintext youtube refresh token sitting next to the
``_d`` dict, because what's in the dict is opaque ciphertext + the key
lives in a separate attribute that goes away at ``shutdown()``.

**Limitations** — be honest about what this is and isn't:

* Python has **no real secure-memory primitives**. We can't ``mlock`` a
  string, we can't guarantee the GC won't have already copied the
  plaintext somewhere we can't reach, and decoded UTF-8 bytes that get
  returned to callers as ``str`` are immutable and live until the GC
  decides otherwise.
* An attacker with **kernel-level read access** (or a debugger attached
  to the live process) defeats this — they can read the Fernet key and
  decrypt everything in one step. This is best-effort residency
  reduction, not an HSM-style hardened guarantee.
* The decrypted plaintext is held briefly as ``bytearray`` while we
  reassemble it; we zeroize the bytearray after decoding, but the
  resulting ``str`` is still immutable.
* ``shutdown()`` overwrites every ciphertext entry with random bytes of
  the same length and then clears the dict. This shrinks the window
  but does not (cannot) guarantee the GC won't have residue elsewhere.

If you need an actual hardened secret store, run the agent under a real
secret manager (OS keyring, sealed container with read-only secrets,
HSM-backed Vault) — don't rely on this.

Surface (mirrors core.secrets_store):
  - ``get_secret(key) -> str | None``
  - ``set_secret(key, value: str) -> None``  (emits ``credentials_updated``)
  - ``delete_secret(key) -> None``           (emits ``credentials_updated``)
  - ``get_blob(key) -> bytes | None``        (UTF-8 encoded view of stored str)
  - ``set_blob(key, data: bytes) -> None``   (decoded UTF-8; emits credentials_updated)
  - ``has_secret(key) -> bool``
  - ``materialize_blob_to_tempfile(key, *, suffix='') -> Iterator[str | None]``
  - ``shutdown() -> None``                   (zeroize + clear; idempotent)

Encoding assumption: the agent receives credentials as JSON strings in the
job-plan envelope, so the public interface is keyed ``str -> str``. The
blob methods are a thin UTF-8 adapter so PlaywrightSession's ``get_blob``
/ ``set_blob`` (which trade in ``bytes``) keep working unchanged.
Non-UTF-8 blobs are not supported (logged + dropped on set).
"""
from __future__ import annotations
import contextlib
import logging
import os
import secrets as _secrets
import tempfile
from typing import Callable, Optional, Iterator

from cryptography.fernet import Fernet, InvalidToken

_EmitFn = Callable[[dict], None]
_log = logging.getLogger(__name__)


def _zeroize_bytearray(ba: bytearray) -> None:
    """Overwrite a bytearray with zeros in-place. Best-effort only —
    Python may have already copied the data elsewhere via the GC.
    """
    for i in range(len(ba)):
        ba[i] = 0


class Shim:
    """In-memory credential shim with Fernet-encrypted at-rest storage."""

    def __init__(self, *, initial: Optional[dict] = None,
                 emit: Optional[_EmitFn] = None) -> None:
        # Per-process Fernet key — never persisted, never logged.
        # A fresh Shim gets a fresh key, which means a re-install via
        # install_as_core_secrets_store between jobs effectively rotates
        # the key and invalidates any leaked ciphertexts from the prior
        # job (they'd be undecryptable anyway, since the key is gone).
        self._key: bytes = Fernet.generate_key()
        self._fernet: Fernet = Fernet(self._key)
        # Values stored as ciphertext bytes. Plaintext only ever lives
        # in short-lived locals inside get_*/materialize_*.
        self._d: dict[str, bytes] = {}
        self._emit: _EmitFn = emit or (lambda _frame: None)
        self._closed: bool = False

        if initial:
            for k, v in initial.items():
                # Encrypt without emitting — initial seeding shouldn't
                # send credentials_updated frames back to the server.
                self._d[k] = self._encrypt_str(v)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _encrypt_str(self, value: str) -> bytes:
        """Encrypt a plaintext str → ciphertext bytes."""
        return self._fernet.encrypt(value.encode("utf-8"))

    def _decrypt_to_str(self, token: bytes) -> Optional[str]:
        """Decrypt ciphertext → plaintext str. Returns None on failure
        (which indicates the key has been rotated / shim shut down)."""
        try:
            plain_bytes = self._fernet.decrypt(token)
        except InvalidToken:
            _log.debug("secrets_shim: decrypt failed (key may have rotated)")
            return None
        # Hold plaintext in a mutable bytearray so we can zeroize it
        # before it leaves scope. The decoded str is still immutable —
        # this is best-effort, see module docstring.
        ba = bytearray(plain_bytes)
        try:
            return ba.decode("utf-8")
        finally:
            _zeroize_bytearray(ba)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_secret(self, key: str) -> Optional[str]:
        token = self._d.get(key)
        if token is None:
            return None
        return self._decrypt_to_str(token)

    def set_secret(self, key: str, value: str) -> None:
        if self._closed:
            _log.warning("secrets_shim.set_secret called after shutdown(); ignoring")
            return
        self._d[key] = self._encrypt_str(value)
        self._emit({"type": "credentials_updated", "key": key, "value": value})

    def delete_secret(self, key: str) -> None:
        # Overwrite the ciphertext before popping (defense in depth — the
        # ciphertext doesn't reveal plaintext, but make the residency
        # story uniform across set / delete / shutdown).
        existing = self._d.get(key)
        if existing is not None:
            self._d[key] = _secrets.token_bytes(len(existing))
        self._d.pop(key, None)
        self._emit({"type": "credentials_updated", "key": key, "value": ""})

    def has_secret(self, key: str) -> bool:
        """True if the shim is currently holding a value for ``key``."""
        return key in self._d

    def get_blob(self, key: str) -> Optional[bytes]:
        """Return the stored value as UTF-8 bytes, or None if unset.

        PlaywrightSession passes Playwright storage_state JSON through
        get_blob/set_blob. We keep the public interface as ``str -> str``
        and transparently encode on the way out.
        """
        val = self.get_secret(key)
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
        val = self.get_secret(key)
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

    def shutdown(self) -> None:
        """Zeroize every stored ciphertext entry and clear the dict.

        Idempotent — calling twice is safe.

        After shutdown:
          * ``set_secret`` is a no-op (logged warning).
          * ``get_secret`` returns ``None`` for every key (dict is empty).
          * The Fernet key is dropped, so any leaked ciphertext from
            before shutdown is now undecryptable.

        Called from ``agent/dispatch.handle_job_plan``'s ``finally`` block
        so credentials don't linger in memory after a job completes.
        """
        if self._closed:
            return
        # Overwrite each ciphertext value with random bytes of equal
        # length before clearing — defense in depth.
        for k in list(self._d.keys()):
            existing = self._d[k]
            self._d[k] = _secrets.token_bytes(len(existing))
        self._d.clear()
        # Drop the Fernet key reference so any future decrypt attempt
        # (e.g. against a leaked ciphertext copy) fails.
        self._key = b""
        # Replace with a fresh, unusable Fernet so attribute access
        # on _fernet doesn't AttributeError — but its key is unrelated
        # to anything previously emitted.
        self._fernet = Fernet(Fernet.generate_key())
        self._closed = True


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
