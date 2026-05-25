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
import shutil
import subprocess
import sys
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _platform_label() -> str:
    s = platform.system().lower()
    return "windows" if s == "windows" else ("macos" if s == "darwin" else s)


def _full_label(arch: str | None) -> str:
    """e.g. 'windows', 'macos', 'macos-arm64', 'macos-intel'.

    A bare 'macos' is the **universal2** fat binary that runs on both archs
    — that's what we ship from CI now. The arm64/intel suffixes are kept
    for backward compatibility with local builds that target a single arch.
    """
    base = _platform_label()
    if base == "macos" and arch and arch != "universal":
        return f"{base}-{arch}"
    return base


def _binary_name(version: str, arch: str | None) -> str:
    """Distribution filename. On macOS this is a .zip of the .app bundle
    (browsers strip the executable bit on raw binary downloads, and a
    bare Mach-O shows as a generic Unix-executable document in Finder);
    Windows keeps the .exe; other platforms get the bare binary.
    """
    plat = _platform_label()
    if plat == "windows":
        ext = ".exe"
    elif plat == "macos":
        ext = ".zip"
    else:
        ext = ""
    return f"dld-agent-{_full_label(arch)}-{version}{ext}"


def _zip_app_bundle(app_path: str, out_zip: str) -> None:
    """Zip dist/dld-agent.app → out_zip preserving symlinks + the
    executable bit on Contents/MacOS/dld-agent.

    Using shutil.make_archive("zip") would lose the executable bit (the
    stdlib zipfile defaults to 0644 file mode), so the user would end
    up with an .app whose inner binary is non-executable. We build the
    archive by hand with explicit external_attr so unzip restores +x.
    """
    if os.path.exists(out_zip):
        os.remove(out_zip)
    app_root = os.path.dirname(app_path.rstrip(os.sep))
    base = os.path.basename(app_path.rstrip(os.sep))
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirnames, filenames in os.walk(app_path, followlinks=False):
            # Add the directory itself so empty directories survive.
            arc_dir = os.path.relpath(dirpath, app_root) + "/"
            zi_dir = zipfile.ZipInfo(arc_dir)
            zi_dir.external_attr = (0o755 << 16) | 0x10  # dir + 755
            zf.writestr(zi_dir, b"")
            for name in filenames:
                src = os.path.join(dirpath, name)
                arc = os.path.relpath(src, app_root)
                # Preserve symlinks verbatim — PyInstaller's .app uses a
                # few inside Contents/Frameworks. Writing the link
                # target as the file body + flagging mode 0o120000
                # tells unzip "this is a symlink".
                if os.path.islink(src):
                    link_target = os.readlink(src)
                    zi = zipfile.ZipInfo(arc)
                    zi.create_system = 3  # unix
                    zi.external_attr = (0o120777 << 16)  # symlink + 0777
                    zf.writestr(zi, link_target)
                else:
                    st = os.stat(src)
                    zi = zipfile.ZipInfo(arc)
                    zi.create_system = 3  # unix
                    zi.external_attr = (st.st_mode & 0o7777) << 16
                    with open(src, "rb") as fh:
                        zf.writestr(zi, fh.read())


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
        choices=["arm64", "intel", "universal"],
        help=(
            "(macOS only) Build target. 'universal' produces a single "
            "universal2 fat binary (runs on Apple Silicon + Intel) — what "
            "CI uses. 'arm64' / 'intel' produce arch-specific binaries; "
            "auto-detected from platform.machine() for local builds."
        ),
    )
    args = ap.parse_args()
    arch = args.arch or _detect_mac_arch()

    # Hand PyInstaller the requested target arch via the spec's env-var
    # hook. PyInstaller ignores --target-arch as a CLI flag when a spec
    # file is in play, so we route through the env.
    build_env = os.environ.copy()
    if _platform_label() == "macos" and arch == "universal":
        build_env["DLD_AGENT_TARGET_ARCH"] = "universal2"

    # Run PyInstaller.
    subprocess.check_call(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "agent.spec"],
        env=build_env,
    )
    plat = _platform_label()
    final_name = _binary_name(args.version, arch)
    final_path = os.path.join("dist", final_name)

    if plat == "macos":
        # PyInstaller's BUNDLE() target emits dist/dld-agent.app alongside
        # dist/dld-agent (the bare binary, which we don't ship). Zip the
        # .app so the executable bit survives a browser download and
        # Finder treats the asset as an application.
        app_path = os.path.join("dist", "dld-agent.app")
        if not os.path.isdir(app_path):
            sys.exit(
                f"Expected {app_path} after PyInstaller run — agent.spec "
                "must have a BUNDLE() block on macOS."
            )
        _zip_app_bundle(app_path, final_path)
        # Drop the bare binary so the upload-artifact glob doesn't pick
        # up an unsigned duplicate. The .app inside our zip is the only
        # macOS deliverable.
        bare = os.path.join("dist", "dld-agent")
        if os.path.exists(bare):
            try:
                os.remove(bare)
            except OSError:
                pass
        # The .app directory has done its job — clean up so the artifact
        # upload doesn't try to traverse it.
        shutil.rmtree(app_path, ignore_errors=True)
    else:
        built = os.path.join(
            "dist", "dld-agent.exe" if plat == "windows" else "dld-agent",
        )
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
