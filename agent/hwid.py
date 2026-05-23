"""Compute a stable per-machine hardware identifier hash.

The raw machine id is never sent off the device; we send sha256 over a
prefixed salt so identical IDs across the fleet are merely correlatable
inside one server (where we already store the hash), not across deploys.

py-machineid handles the platform-specific details:
  * Linux: /etc/machine-id, /var/lib/dbus/machine-id
  * macOS: IOPlatformUUID
  * Windows: MachineGuid from the registry

It's the most reliable cross-platform option; pyfsutil/uuid.getnode() drift
between reboots or between LAN/WiFi interfaces.

If py-machineid isn't available or raises (rare: unusual platforms, locked
permissions on the registry path), we fall back to a stable hash derived
from hostname + platform string. The fallback is documented in the test
suite — production deploys must `pip install py-machineid`.
"""
from __future__ import annotations

import hashlib
import logging
import platform
import socket

_SALT = b"dld-hwid:"
_log = logging.getLogger(__name__)


def _fallback_seed() -> str:
    """Hash a deterministic-ish blob of stable host info.

    Won't survive reinstalls (gethostname() may change on Windows after
    AD join, platform() may rev with kernel updates), but it's stable
    enough for the immediate session and never empty. Logged at WARNING
    so on-call sees the degraded mode.
    """
    return "|".join((
        socket.gethostname() or "",
        platform.platform() or "",
        platform.node() or "",
    ))


def compute_hwid_hash() -> str:
    """Return the 64-char sha256 hex digest of the machine's stable HWID.

    Always returns a 64-character lowercase hex string. The salt prefix
    prevents the digest from coinciding with the bare sha256 of the raw
    machine-id (which is what py-machineid's own `hashed_id` does), so
    that a leaked DB row can't be cross-referenced against any other
    fleet using the same hashing convention.
    """
    raw: str
    try:
        import machineid  # type: ignore[import-not-found]
        raw = machineid.id()
        if not raw:
            raise RuntimeError("machineid.id() returned empty")
    except Exception as exc:  # noqa: BLE001 — *any* failure → degraded path
        _log.warning(
            "py-machineid unavailable or failed (%s); falling back to "
            "hostname+platform-derived HWID — install py-machineid for "
            "a stable id across reboots/reinstalls", exc,
        )
        raw = _fallback_seed()
    return hashlib.sha256(_SALT + raw.encode("utf-8")).hexdigest()
