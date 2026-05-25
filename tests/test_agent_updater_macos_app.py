"""macOS .app-bundle update path for ``agent.updater``.

The updater now ships the .app bundle as a .zip (so the executable bit
survives browser downloads and Finder treats the asset as an
application). These tests cover the helpers that route a downloaded
.zip into a real install on disk; we don't run them under macOS-only
gating because the helpers themselves are pure filesystem code that
works on any OS that supports zipfile + symlinks.

CI runs on Windows + Linux; the symlink test is skipped on Windows
because plain user accounts can't create symlinks there without the
SeCreateSymbolicLinkPrivilege.
"""
from __future__ import annotations

import base64
import os
import stat
import sys
import zipfile

import pytest

from agent import updater


# ── _find_app_bundle_root ────────────────────────────────────────────


def test_find_app_bundle_root_finds_enclosing_dot_app(tmp_path):
    app = tmp_path / "DLD Agent.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    exe = app / "Contents" / "MacOS" / "dld-agent"
    exe.write_bytes(b"x")
    assert updater._find_app_bundle_root(str(exe)) == str(app)


def test_find_app_bundle_root_returns_none_for_bare_binary(tmp_path):
    exe = tmp_path / "dld-agent"
    exe.write_bytes(b"x")
    assert updater._find_app_bundle_root(str(exe)) is None


def test_find_app_bundle_root_handles_nested_app(tmp_path):
    # Some installs end up inside another .app's Frameworks dir; we
    # want the CLOSEST .app ancestor (the inner one), not the outer.
    outer = tmp_path / "Wrapper.app"
    inner = outer / "Contents" / "Frameworks" / "DLD Agent.app"
    (inner / "Contents" / "MacOS").mkdir(parents=True)
    exe = inner / "Contents" / "MacOS" / "dld-agent"
    exe.write_bytes(b"x")
    assert updater._find_app_bundle_root(str(exe)) == str(inner)


# ── _extract_app_from_zip ───────────────────────────────────────────


def _build_app_zip(zip_path: str, app_name: str = "DLD Agent.app",
                   *, with_symlink: bool = False) -> None:
    """Hand-rolled .app inside a zip that mirrors what build_agent.py
    emits: a Contents/MacOS/dld-agent with 0755, plus optionally a
    symlink to exercise the symlink-preservation path."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Dir entries for round-trip parity (empty bodies, mode 0755).
        for d in (f"{app_name}/", f"{app_name}/Contents/",
                  f"{app_name}/Contents/MacOS/"):
            zi = zipfile.ZipInfo(d)
            zi.external_attr = (0o755 << 16) | 0x10
            zf.writestr(zi, b"")
        # The inner binary, executable.
        zi = zipfile.ZipInfo(f"{app_name}/Contents/MacOS/dld-agent")
        zi.create_system = 3  # unix
        zi.external_attr = (0o755 << 16)
        zf.writestr(zi, b"#!/bin/sh\necho new\n")
        # Info.plist so the .app looks plausible (not strictly required
        # for the test but mirrors the real shape).
        zi = zipfile.ZipInfo(f"{app_name}/Contents/Info.plist")
        zi.create_system = 3
        zi.external_attr = (0o644 << 16)
        zf.writestr(zi, b"<plist/>\n")
        if with_symlink:
            zi = zipfile.ZipInfo(f"{app_name}/Contents/MacOS/dld-agent-link")
            zi.create_system = 3
            zi.external_attr = (0o120777 << 16)  # symlink + 0777
            zf.writestr(zi, b"dld-agent")


def test_extract_app_returns_app_path(tmp_path):
    zip_path = tmp_path / "payload.zip"
    _build_app_zip(str(zip_path))
    dest = tmp_path / "extract"
    dest.mkdir()
    out = updater._extract_app_from_zip(str(zip_path), str(dest))
    assert out is not None
    assert out.endswith("DLD Agent.app")
    inner = os.path.join(out, "Contents", "MacOS", "dld-agent")
    assert os.path.isfile(inner)


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="Posix mode bits aren't honored on NTFS — chmod is a no-op there.",
)
def test_extract_app_preserves_executable_bit(tmp_path):
    zip_path = tmp_path / "payload.zip"
    _build_app_zip(str(zip_path))
    dest = tmp_path / "extract"
    dest.mkdir()
    out = updater._extract_app_from_zip(str(zip_path), str(dest))
    inner = os.path.join(out, "Contents", "MacOS", "dld-agent")
    mode = os.stat(inner).st_mode
    assert mode & stat.S_IXUSR, "inner binary must keep +x after extract"


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="symlink creation requires elevated privileges on Windows.",
)
def test_extract_app_preserves_symlinks(tmp_path):
    zip_path = tmp_path / "payload.zip"
    _build_app_zip(str(zip_path), with_symlink=True)
    dest = tmp_path / "extract"
    dest.mkdir()
    out = updater._extract_app_from_zip(str(zip_path), str(dest))
    link = os.path.join(out, "Contents", "MacOS", "dld-agent-link")
    assert os.path.islink(link), "symlink members must extract as symlinks"
    assert os.readlink(link) == "dld-agent"


def test_extract_app_rejects_zip_slip(tmp_path):
    """A zip with ../etc/passwd-style entries must be refused."""
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../escape.txt", b"pwned")
    dest = tmp_path / "extract"
    dest.mkdir()
    assert updater._extract_app_from_zip(str(zip_path), str(dest)) is None
    # And the escape file did not get written outside dest.
    assert not (tmp_path / "escape.txt").exists()


def test_extract_app_returns_none_when_no_app_in_zip(tmp_path):
    zip_path = tmp_path / "noapp.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("just/a/file.txt", b"hi")
    dest = tmp_path / "extract"
    dest.mkdir()
    assert updater._extract_app_from_zip(str(zip_path), str(dest)) is None


# ── _swap_macos_app ─────────────────────────────────────────────────


def test_swap_macos_app_replaces_current_and_cleans_old(tmp_path):
    current = tmp_path / "DLD Agent.app"
    (current / "Contents" / "MacOS").mkdir(parents=True)
    (current / "Contents" / "MacOS" / "dld-agent").write_text("OLD")

    incoming = tmp_path / "incoming" / "DLD Agent.app"
    (incoming / "Contents" / "MacOS").mkdir(parents=True)
    (incoming / "Contents" / "MacOS" / "dld-agent").write_text("NEW")

    returned = updater._swap_macos_app(str(incoming), str(current))
    assert returned == str(current)
    # The new bytes are at the original path now.
    assert (current / "Contents" / "MacOS" / "dld-agent").read_text() == "NEW"
    # The .old shouldn't linger after a successful swap.
    assert not (tmp_path / "DLD Agent.app.old").exists()


def test_swap_macos_app_replaces_preexisting_dot_old(tmp_path):
    # A leftover .old from a prior crashed update must be cleared, not
    # made the swap fail.
    current = tmp_path / "DLD Agent.app"
    (current / "Contents" / "MacOS").mkdir(parents=True)
    (current / "Contents" / "MacOS" / "dld-agent").write_text("OLD")
    leftover = tmp_path / "DLD Agent.app.old"
    leftover.mkdir()
    (leftover / "stale").write_text("from-last-update")

    incoming = tmp_path / "incoming" / "DLD Agent.app"
    (incoming / "Contents" / "MacOS").mkdir(parents=True)
    (incoming / "Contents" / "MacOS" / "dld-agent").write_text("NEW")

    updater._swap_macos_app(str(incoming), str(current))
    assert (current / "Contents" / "MacOS" / "dld-agent").read_text() == "NEW"


# ── download_and_verify .zip URL suffix ─────────────────────────────


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub_pem


def test_download_and_verify_saves_zip_suffix_for_zip_url(monkeypatch, tmp_path):
    """A .zip URL must land at new-binary.zip so apply_update can route
    macOS payloads through the unzip+app-swap path."""
    from agent import signing
    priv, pub_pem = _keypair()
    payload = b"PAYLOAD-BYTES"
    build = {
        "url": "https://example/agent/releases/dld-agent-macos-0.7.0.zip",
        "sha256": signing.sha256(payload),
        "signature_b64": base64.b64encode(priv.sign(payload)).decode("ascii"),
    }
    monkeypatch.setattr(updater, "_fetch_bytes", lambda url: payload)
    monkeypatch.setattr(updater, "_load_pubkey", lambda: pub_pem)
    out = updater.download_and_verify(build, dest_dir=str(tmp_path))
    assert out is not None
    assert out.endswith(".zip"), f"expected .zip suffix, got {out!r}"


def test_download_and_verify_no_suffix_for_exe_url(monkeypatch, tmp_path):
    """Windows .exe URLs keep landing at the un-suffixed slot — the
    Windows branch of apply_update doesn't care about the extension."""
    from agent import signing
    priv, pub_pem = _keypair()
    payload = b"WIN-BYTES"
    build = {
        "url": "https://example/agent/releases/dld-agent-windows-0.7.0.exe",
        "sha256": signing.sha256(payload),
        "signature_b64": base64.b64encode(priv.sign(payload)).decode("ascii"),
    }
    monkeypatch.setattr(updater, "_fetch_bytes", lambda url: payload)
    monkeypatch.setattr(updater, "_load_pubkey", lambda: pub_pem)
    out = updater.download_and_verify(build, dest_dir=str(tmp_path))
    assert out is not None
    assert not out.endswith(".zip")
