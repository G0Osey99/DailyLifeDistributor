"""Auto-update for the agent.

Flow (frozen / PyInstaller bundle only):
  1. Fetch manifest from <server>/agent/releases/manifest.json
  2. Compare manifest.version to agent._version.__version__
  3. If newer: download the platform's binary, verify sha256 + ed25519 signature
     against the baked public key (agent/release_pubkey.pem), then apply.
  4. Apply: write the new binary in place of the running one (Windows uses
     a rename pattern), spawn it, exit.

From-source runs (`sys.frozen` unset) skip updates entirely so developers
aren't surprised.

Any failure logs and returns - the agent NEVER bricks itself trying to update.
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from importlib import resources

import requests

from agent._version import __version__ as _CURRENT_VERSION
from agent.signing import sha256, verify_signature

log = logging.getLogger(__name__)


def _current_version() -> str:
    return _CURRENT_VERSION


def _current_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform  # unsupported -> falls through naturally


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _load_pubkey() -> bytes:
    """Load the bundled release public key. Works both from source and from a
    PyInstaller bundle (importlib.resources handles both)."""
    return resources.files("agent").joinpath("release_pubkey.pem").read_bytes()


def _fetch_with_retry(url: str, *, timeout: float, attempts: int = 2):
    """GET with one retry on connection error. Returns the response.

    The update check runs once at agent boot — a single transient flake
    shouldn't lock the agent into another launch cycle on the old
    version. We don't retry on 4xx (those are deterministic) and we
    don't try harder than twice (boot must stay snappy).
    """
    import random
    import time
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.HTTPError:
            # Server reachable, but answered 4xx/5xx — don't retry
            # (5xx is also usually not transient for a static asset).
            raise
        except requests.RequestException as e:
            last_exc = e
            if attempt < attempts:
                time.sleep(0.5 + random.random() * 0.5)
                continue
            raise
    # Defensive — we either returned or raised above.
    raise last_exc  # type: ignore[misc]


def _fetch_manifest(server_url: str) -> dict:
    r = _fetch_with_retry(
        server_url.rstrip("/") + "/agent/releases/manifest.json",
        timeout=15,
    )
    return r.json()


def _fetch_bytes(url: str) -> bytes:
    r = _fetch_with_retry(url, timeout=60)
    return r.content


def _is_newer(remote: str, local: str) -> bool:
    """Tuple-compare dotted versions. Both must be N.N.N strings."""
    def parts(v):
        return tuple(int(x) for x in v.split(".") if x.isdigit())
    return parts(remote) > parts(local)


def check_for_update(server_url: str):
    """Return (latest_version, build_dict) or None if not newer / unavailable."""
    try:
        manifest = _fetch_manifest(server_url)
    except Exception as e:
        log.debug("update check: fetch_manifest failed: %s", e)
        return None
    remote = str(manifest.get("version", ""))
    if not remote or not _is_newer(remote, _current_version()):
        return None
    build = (manifest.get("builds") or {}).get(_current_platform())
    if not build:
        log.debug("update check: no build for platform %s", _current_platform())
        return None
    return remote, build


def download_and_verify(build: dict, dest_dir: str | None = None) -> str | None:
    """Download the build, verify sha256 + signature, return temp file path or None."""
    url = build.get("url")
    expected_sha = build.get("sha256", "")
    sig_b64 = build.get("signature_b64", "")
    if not (url and expected_sha and sig_b64):
        log.warning("update: build dict missing url/sha256/signature_b64")
        return None
    try:
        data = _fetch_bytes(url)
    except Exception as e:
        log.warning("update: download failed: %s", e)
        return None
    if sha256(data) != expected_sha:
        log.warning("update: sha256 mismatch")
        return None
    try:
        sig = base64.b64decode(sig_b64)
    except Exception:
        log.warning("update: signature_b64 is not valid base64")
        return None
    pubkey_pem = _load_pubkey()
    if not verify_signature(data, sig, pubkey_pem):
        log.warning("update: signature verification failed")
        return None
    dest = dest_dir or tempfile.mkdtemp(prefix="dld-agent-update-")
    out_path = os.path.join(dest, "new-binary")
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path


def apply_update(new_binary_path: str) -> None:
    """OS-specific swap-and-relaunch. Caller has already verified the payload."""
    current = sys.executable  # PyInstaller --onefile: the running .exe / Mach-O
    plat = _current_platform()
    if plat == "windows":
        # Can't delete a running .exe but CAN rename it.
        old = current + ".old"
        try:
            if os.path.exists(old):
                os.remove(old)
        except OSError:
            pass
        os.replace(current, old)               # current -> .old
        shutil.move(new_binary_path, current)  # new -> current
        # Spawn the new binary detached, then exit ourselves.
        subprocess.Popen([current], close_fds=True)
        log.info("update: applied; relaunching as %s", current)
        os._exit(0)
    elif plat == "macos":
        shutil.copyfile(new_binary_path, current)
        os.chmod(current, 0o755)
        # Strip the quarantine bit so the relaunched binary isn't Gatekeeper-prompted.
        subprocess.run(["xattr", "-d", "com.apple.quarantine", current],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen([current], close_fds=True)
        log.info("update: applied; relaunching as %s", current)
        os._exit(0)
    else:
        log.info("update: platform %s not supported for in-place swap; skipping", plat)


def check_and_apply(server_url: str) -> None:
    """One-shot: skip if not frozen; else check, download/verify, apply."""
    if not _is_frozen():
        log.debug("update: not frozen (dev run); skipping")
        return
    result = check_for_update(server_url)
    if not result:
        return
    version, build = result
    log.info("update: %s available (running %s); downloading", version, _current_version())
    new_path = download_and_verify(build)
    if not new_path:
        log.info("update: download/verify failed; staying on %s", _current_version())
        return
    try:
        apply_update(new_path)
    except Exception:  # noqa: BLE001
        log.exception("update: apply_update raised; staying on %s", _current_version())
