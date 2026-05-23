"""Phase γ Task 4: QR-code PNG render."""
from __future__ import annotations

from core.qrcode_render import render_provisioning_qr_png


def test_render_returns_png_bytes():
    data = render_provisioning_qr_png(
        "otpauth://totp/Daily%20Life%20Distributor:alice?secret=ABCD&issuer=Daily%20Life%20Distributor"
    )
    assert isinstance(data, (bytes, bytearray))
    # PNG signature
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) > 200
