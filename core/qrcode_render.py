"""Render an otpauth:// provisioning URI as PNG bytes."""
from __future__ import annotations

import io

import qrcode


def render_provisioning_qr_png(uri: str) -> bytes:
    """Return PNG-encoded bytes for the given provisioning URI."""
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
