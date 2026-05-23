"""Ensures the multi-tenant deps are present in requirements.txt."""
from pathlib import Path

REQ = Path(__file__).resolve().parent.parent / "requirements.txt"

def test_argon2_cffi_pinned():
    contents = REQ.read_text(encoding="utf-8")
    assert "argon2-cffi>=23" in contents, "argon2-cffi must be pinned >=23"

def test_pyotp_pinned():
    contents = REQ.read_text(encoding="utf-8")
    assert "pyotp>=2.9" in contents

def test_qrcode_pinned():
    contents = REQ.read_text(encoding="utf-8")
    assert "qrcode[pil]>=7.4" in contents

def test_resend_pinned():
    contents = REQ.read_text(encoding="utf-8")
    assert "resend>=0.7" in contents

def test_itsdangerous_pinned():
    contents = REQ.read_text(encoding="utf-8")
    assert "itsdangerous>=2.1" in contents

def test_flask_wtf_pinned():
    contents = REQ.read_text(encoding="utf-8")
    assert "flask-wtf>=1.2" in contents


def test_freezegun_pinned():
    dev = (Path(__file__).resolve().parent.parent / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "freezegun>=1.4" in dev
