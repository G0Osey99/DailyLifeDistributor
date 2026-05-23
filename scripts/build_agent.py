"""Local build wrapper for the agent.

Usage (after generating the release keypair and storing the private key in
AGENT_RELEASE_PRIVATE_KEY env var or a file via --key-file):

    python scripts/build_agent.py --version 0.2.0

Runs PyInstaller, signs the bundle, and prints a JSON fragment suitable for
inclusion in manifest.json:
    {"platform": "...", "filename": "...", "sha256": "...", "signature_b64": "..."}

In CI (GHA), the matrix job sets AGENT_RELEASE_PRIVATE_KEY from a secret.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import subprocess
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _platform_label() -> str:
    s = platform.system().lower()
    return "windows" if s == "windows" else ("macos" if s == "darwin" else s)


def _full_label(arch: str | None) -> str:
    """e.g. 'windows', 'macos-arm64', 'macos-intel'.

    ``arch`` is the value passed in via --arch (or auto-detected on macOS).
    For Windows we never append an arch because GHA only ships an x64 runner.
    """
    base = _platform_label()
    if base == "macos" and arch:
        return f"{base}-{arch}"
    return base


def _binary_name(version: str, arch: str | None) -> str:
    ext = ".exe" if _platform_label() == "windows" else ""
    return f"dld-agent-{_full_label(arch)}-{version}{ext}"


def _detect_mac_arch() -> str | None:
    """Return 'arm64' / 'intel' on macOS, else None. The CI matrix sets --arch
    explicitly; this is the local-build fallback."""
    if _platform_label() != "macos":
        return None
    m = platform.machine().lower()
    return "arm64" if m in ("arm64", "aarch64") else "intel"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="semver-ish, e.g. 0.2.0")
    ap.add_argument("--key-file", help="path to ed25519 private key PEM (else AGENT_RELEASE_PRIVATE_KEY env)")
    ap.add_argument(
        "--arch",
        choices=["arm64", "intel"],
        help="(macOS only) arch suffix baked into filename + manifest. "
             "Auto-detected from platform.machine() if omitted.",
    )
    args = ap.parse_args()
    arch = args.arch or _detect_mac_arch()

    # Run PyInstaller.
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--clean",
                           "--noconfirm", "agent.spec"])
    built = os.path.join("dist", "dld-agent.exe" if _platform_label() == "windows" else "dld-agent")
    final_name = _binary_name(args.version, arch)
    final_path = os.path.join("dist", final_name)
    os.replace(built, final_path)

    # Sign.
    if args.key_file:
        priv_pem = open(args.key_file, "rb").read()
    else:
        priv_pem = os.environ.get("AGENT_RELEASE_PRIVATE_KEY", "").encode("ascii")
    if not priv_pem:
        sys.exit("Missing private key: pass --key-file or set AGENT_RELEASE_PRIVATE_KEY")
    priv = serialization.load_pem_private_key(priv_pem, password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        sys.exit("Provided key is not an ed25519 private key")
    data = open(final_path, "rb").read()
    sig = priv.sign(data)
    digest = hashlib.sha256(data).hexdigest()

    print(json.dumps({
        "platform": _full_label(arch),
        "arch": arch,
        "filename": final_name,
        "sha256": digest,
        "signature_b64": base64.b64encode(sig).decode("ascii"),
    }, indent=2))


if __name__ == "__main__":
    main()
