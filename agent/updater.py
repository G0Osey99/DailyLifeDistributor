"""Auto-update for the agent.

Flow (frozen / PyInstaller bundle only):
  1. Fetch manifest from <server>/agent/releases/manifest.json
  2. Compare manifest.version to agent._version.__version__
  3. If newer: download the platform's payload (.exe on Windows, .zip
     containing a .app bundle on macOS), verify sha256 + ed25519
     signature against the baked public key (agent/release_pubkey.pem),
     then apply.
  4. Apply: write the new binary in place of the running one (Windows
     uses a rename pattern; macOS unzips and atomically swaps the .app
     directory), spawn it, exit.

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
import zipfile
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
    # Preserve a .zip suffix on the saved file so apply_update can route
    # macOS .app-bundle payloads through the unzip path. Non-zip URLs
    # (e.g. Windows .exe) land in the original `new-binary` slot and the
    # bare-binary swap path handles them as before.
    suffix = ".zip" if url.lower().split("?", 1)[0].endswith(".zip") else ""
    out_path = os.path.join(dest, "new-binary" + suffix)
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path


def _find_app_bundle_root(exe_path: str) -> str | None:
    """Return the .app directory containing ``exe_path``, or None.

    macOS PyInstaller bundles put the binary at
    ``<app>/Contents/MacOS/dld-agent``; walk up until we hit a path that
    ends with ``.app``. Returns None when running from a legacy bare
    binary (no .app ancestor) or on non-macOS.
    """
    p = os.path.abspath(exe_path)
    while p and p != os.path.dirname(p):
        if p.endswith(".app"):
            return p
        p = os.path.dirname(p)
    return None


def _extract_app_from_zip(zip_path: str, dest_dir: str) -> str | None:
    """Extract ``zip_path`` into ``dest_dir`` and return the path to the
    first ``*.app`` directory at its root, or None if the zip's shape is
    wrong.

    Preserves the executable bit + symlinks (PyInstaller .app bundles
    use both inside Contents/Frameworks). Without symlink preservation
    the relaunched .app would crash on framework load.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            # Path-traversal guard: refuse any entry that resolves
            # outside dest_dir. A malicious zip with ../etc/passwd would
            # otherwise let an MITM scribble files anywhere the agent
            # can write; signature verification upstream is the primary
            # defense but this is a defense-in-depth.
            target = os.path.realpath(os.path.join(dest_dir, member.filename))
            if not target.startswith(os.path.realpath(dest_dir) + os.sep):
                log.warning("update: zip member escapes dest dir: %r",
                            member.filename)
                return None
            mode = (member.external_attr >> 16) & 0xFFFF
            is_symlink = (mode & 0o170000) == 0o120000
            if is_symlink:
                link_target = zf.read(member).decode("utf-8")
                # Ensure parent exists, then create the symlink.
                os.makedirs(os.path.dirname(target), exist_ok=True)
                try:
                    os.symlink(link_target, target)
                except FileExistsError:
                    os.remove(target)
                    os.symlink(link_target, target)
            else:
                zf.extract(member, dest_dir)
                # zipfile.extract() ignores the unix mode bits — reapply
                # the executable bit explicitly so Contents/MacOS/dld-agent
                # can actually run.
                if mode & 0o111:
                    os.chmod(target, mode & 0o7777)
    for name in os.listdir(dest_dir):
        if name.endswith(".app"):
            return os.path.join(dest_dir, name)
    return None


def _swap_macos_app(new_app: str, current_app: str) -> str:
    """Replace ``current_app`` with ``new_app`` atomically and return
    the path that the relaunch should ``open``.

    The swap is two ``os.rename`` calls: current → .old, new → current.
    A subsequent run can clean up the .old (we delete it best-effort
    here too). Returns the path the caller should pass to ``open``.
    """
    old = current_app + ".old"
    if os.path.exists(old):
        shutil.rmtree(old, ignore_errors=True)
    os.rename(current_app, old)
    try:
        shutil.move(new_app, current_app)
    except Exception:
        # Roll the .old back into place so we don't leave the install
        # half-removed if the move blows up mid-flight.
        os.rename(old, current_app)
        raise
    # Best-effort cleanup of the prior .old left by an earlier update.
    shutil.rmtree(old, ignore_errors=True)
    return current_app


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
        is_zip = new_binary_path.lower().endswith(".zip")
        app_root = _find_app_bundle_root(current)
        if is_zip and app_root:
            # New-style install: swap the whole .app bundle and relaunch
            # via `open`, which preserves Launch Services context and the
            # dock icon.
            extract_dir = tempfile.mkdtemp(prefix="dld-agent-extract-")
            try:
                new_app = _extract_app_from_zip(new_binary_path, extract_dir)
                if not new_app:
                    log.warning("update: zip did not contain a .app bundle; skipping")
                    return
                swapped = _swap_macos_app(new_app, app_root)
            finally:
                shutil.rmtree(extract_dir, ignore_errors=True)
            # Strip quarantine off the swapped bundle so the relaunched
            # .app isn't Gatekeeper-prompted on the next launch.
            subprocess.run(
                ["xattr", "-dr", "com.apple.quarantine", swapped],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.Popen(["open", swapped], close_fds=True)
            log.info("update: applied (.app swap); relaunching %s", swapped)
            os._exit(0)
        if is_zip and not app_root:
            # Legacy bare-binary install. Extract the .app, dig out the
            # inner binary, and swap it in place so the user still gets
            # the upgrade without having to re-download manually. They
            # won't get the .app niceties until they re-download from
            # the dashboard, but the agent stays current.
            extract_dir = tempfile.mkdtemp(prefix="dld-agent-extract-")
            try:
                new_app = _extract_app_from_zip(new_binary_path, extract_dir)
                if not new_app:
                    log.warning("update: zip did not contain a .app bundle; skipping")
                    return
                inner = os.path.join(new_app, "Contents", "MacOS", "dld-agent")
                if not os.path.isfile(inner):
                    log.warning("update: .app missing Contents/MacOS/dld-agent; skipping")
                    return
                shutil.copyfile(inner, current)
            finally:
                shutil.rmtree(extract_dir, ignore_errors=True)
            os.chmod(current, 0o755)
            subprocess.run(
                ["xattr", "-d", "com.apple.quarantine", current],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.Popen([current], close_fds=True)
            log.info("update: applied (legacy binary swap); relaunching %s", current)
            os._exit(0)
        # Old-style payload (bare binary, no .zip suffix). Path stays
        # identical to pre-v0.7 behavior so a server that's still
        # serving raw binaries continues to work.
        shutil.copyfile(new_binary_path, current)
        os.chmod(current, 0o755)
        subprocess.run(
            ["xattr", "-d", "com.apple.quarantine", current],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
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
