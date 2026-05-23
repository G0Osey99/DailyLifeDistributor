"""Friendly hostname collection for the device picker UI.

The dashboard shows this string to the user, so we trim cosmetic noise
(macOS appends ".local" to mDNS hostnames, Windows may leave trailing
whitespace from a clipboard-pasted name) and cap the length so a
malicious or accidentally-very-long hostname can't break the dropdown
layout server-side.

Cap is 64 chars: comfortably above any realistic hostname (RFC 1035
limits a DNS label to 63 chars + dot) but well under any DB or HTTP
header limit we care about.
"""
from __future__ import annotations

import socket

_MAX_HOSTNAME_LEN = 64


def get_friendly_hostname() -> str:
    """Return a UI-friendly hostname for this device.

    Steps:
      1. socket.gethostname(); fall back to "device" if empty.
      2. Strip whitespace.
      3. Drop a trailing ".local" (mDNS suffix) — case-insensitive.
      4. Truncate to _MAX_HOSTNAME_LEN characters.
    """
    raw = (socket.gethostname() or "device").strip()
    # Case-insensitive .local suffix strip — "Studio.LOCAL" → "Studio".
    if raw.lower().endswith(".local"):
        raw = raw[:-len(".local")]
    return raw[:_MAX_HOSTNAME_LEN] or "device"
