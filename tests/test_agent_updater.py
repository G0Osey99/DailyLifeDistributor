import base64
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from agent import updater


def _keypair():
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub_pem


def _make_manifest(version: str, filename: str, binary: bytes, priv, platform: str):
    from agent import signing
    sig = priv.sign(binary)
    return {
        "version": version,
        "builds": {
            platform: {
                "url": f"https://server.example/agent/releases/{filename}",
                "sha256": signing.sha256(binary),
                "signature_b64": base64.b64encode(sig).decode("ascii"),
            }
        },
    }


def test_check_returns_none_when_version_not_newer(monkeypatch):
    priv, _ = _keypair()
    manifest = _make_manifest("0.1.0", "x.exe", b"DATA", priv, "windows")
    monkeypatch.setattr(updater, "_fetch_manifest", lambda url: manifest)
    monkeypatch.setattr(updater, "_current_version", lambda: "0.1.0")
    monkeypatch.setattr(updater, "_current_platform", lambda: "windows")
    assert updater.check_for_update("https://server.example") is None


def test_check_returns_build_when_newer(monkeypatch):
    priv, _ = _keypair()
    manifest = _make_manifest("0.2.0", "x.exe", b"DATA", priv, "windows")
    monkeypatch.setattr(updater, "_fetch_manifest", lambda url: manifest)
    monkeypatch.setattr(updater, "_current_version", lambda: "0.1.0")
    monkeypatch.setattr(updater, "_current_platform", lambda: "windows")
    result = updater.check_for_update("https://server.example")
    assert result is not None
    version, build = result
    assert version == "0.2.0"
    assert isinstance(build["sha256"], str) and len(build["sha256"]) == 64
    assert build["url"].endswith("x.exe")


def test_download_and_verify_accepts_valid_payload(monkeypatch, tmp_path):
    priv, pub_pem = _keypair()
    binary = b"NEW_BINARY_BYTES"
    manifest = _make_manifest("0.2.0", "x.exe", binary, priv, "windows")
    build = manifest["builds"]["windows"]
    monkeypatch.setattr(updater, "_fetch_bytes", lambda url: binary)
    monkeypatch.setattr(updater, "_load_pubkey", lambda: pub_pem)
    out = updater.download_and_verify(build, dest_dir=str(tmp_path))
    assert out is not None
    assert open(out, "rb").read() == binary


def test_download_and_verify_rejects_tampered_payload(monkeypatch, tmp_path):
    priv, pub_pem = _keypair()
    manifest = _make_manifest("0.2.0", "x.exe", b"GOOD", priv, "windows")
    build = manifest["builds"]["windows"]
    monkeypatch.setattr(updater, "_fetch_bytes", lambda url: b"TAMPERED")
    monkeypatch.setattr(updater, "_load_pubkey", lambda: pub_pem)
    assert updater.download_and_verify(build, dest_dir=str(tmp_path)) is None


def test_download_and_verify_rejects_bad_signature(monkeypatch, tmp_path):
    priv, pub_pem = _keypair()
    other_priv, _ = _keypair()
    binary = b"X"
    manifest = _make_manifest("0.2.0", "x.exe", binary, other_priv, "windows")
    build = manifest["builds"]["windows"]
    monkeypatch.setattr(updater, "_fetch_bytes", lambda url: binary)
    monkeypatch.setattr(updater, "_load_pubkey", lambda: pub_pem)
    assert updater.download_and_verify(build, dest_dir=str(tmp_path)) is None


def test_check_and_apply_skips_when_not_frozen(monkeypatch):
    # From-source runs (no sys.frozen) must skip updates entirely.
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    called = {"check": 0}
    monkeypatch.setattr(updater, "check_for_update",
                        lambda u: called.__setitem__("check", called["check"] + 1) or None)
    updater.check_and_apply("https://server.example")
    assert called["check"] == 0
