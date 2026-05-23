# Hybrid Upload Agent — Phase 2b: Auto-Update + Packaging

> **Status:** Shipped on 2026-05-23 (consolidated in the `codebase-completion-pass` branch — see git history for the actual per-commit work). The `- [ ]` checkboxes below are TDD step artifacts kept as-is for reference; all steps were executed.

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** A self-updating cross-OS agent. The maintainer pushes `git tag agent-v0.x.y`, GHA builds + signs Windows and macOS bundles, uploads them to the VPS; every running agent self-updates on next startup. This is the post-internship-handoff enabler.

**Architecture:**
- ed25519 signature with a baked-in public key (private key only in GHA secret) — the security boundary that lets us serve binaries publicly from the VPS without auth.
- VPS hosts the manifest + binaries at `/agent/releases/*` (public; gated behind `HYBRID_AGENT_ENABLED`). A host directory is bind-mounted into the container at `/data/releases/`.
- Agent on startup (when running as a frozen PyInstaller bundle) fetches the manifest, compares versions, downloads + verifies + applies. From-source runs skip updates so developers aren't surprised.
- Windows can't replace a running .exe directly: rename the running binary to `.old`, drop the new one in its place, spawn it, exit. macOS overwrites in place and clears the quarantine xattr.

**Tech Stack:** Python 3.11+, `cryptography` (already pinned ≥43), PyInstaller (build-time only), GitHub Actions, `pytest`.

---

## File Structure

**Versioning + crypto (Task 1):**
- `agent/_version.py` — `__version__ = "0.1.0"` (single source of truth).
- `agent/signing.py` — `verify_signature(data, sig, pubkey_pem)`, `sha256(data)`.
- `agent/release_pubkey.pem` — committed public key (placeholder until Task 2 generates the real one).
- `scripts/generate_release_keypair.py` — one-off; prints the private key, writes the public.
- `tests/test_agent_signing.py`.

**Server release endpoints (Task 2):**
- `blueprints/agent.py` — *modify*: add `/agent/releases/manifest.json` and `/agent/releases/<filename>` routes.
- `app.py` — *modify*: add the new endpoints to the gated `_PUBLIC_ENDPOINTS.update(...)` block.
- `core/release_store.py` — *create*: small helpers for `manifest_path()`, `binary_path(filename)` with path-traversal guards.
- `tests/test_release_routes.py`.

**Agent updater (Task 3):**
- `agent/updater.py` — `check_for_update`, `download_and_verify`, `apply_update`, `check_and_apply` (the orchestrator).
- `agent/main.py` — *modify*: call `updater.check_and_apply()` on startup.
- `tests/test_agent_updater.py` — mocked HTTP + a real on-the-fly ed25519 keypair so the verification path is genuinely exercised.

**Build infrastructure (Task 4):**
- `scripts/build_agent.py` — local build wrapper: runs PyInstaller, computes sha256, signs the binary, prints/writes a per-OS manifest fragment.
- `agent.spec` — PyInstaller spec (single-file, hidden imports for `core.file_scanner`).
- `.github/workflows/release-agent.yml` — matrix build on tag push, SCP to VPS.
- `deploy/docker-compose.yml` — *modify*: add the bind mount.
- `docs/release-runbook.md` — how to cut a release; one-time secret setup.

**Message envelope (unchanged):** `{"v":1,"type":"...","payload":{...}}`.

---

### Task 1: Versioning + ed25519 signing primitives + keypair generator

**Files:**
- Create: `agent/_version.py`, `agent/signing.py`, `scripts/generate_release_keypair.py`
- Create (placeholder, committed): `agent/release_pubkey.pem`
- Test: `tests/test_agent_signing.py`

- [ ] **Step 1: `agent/_version.py`**

```python
"""Single source of truth for the agent version. Bumped per release."""
__version__ = "0.1.0"
```

- [ ] **Step 2: write the failing test** `tests/test_agent_signing.py`

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from agent import signing


def _fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub_pem


def test_sha256_hex_of_bytes():
    assert signing.sha256(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert signing.sha256(b"hello") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_verify_signature_accepts_real_signature():
    priv, pub_pem = _fresh_keypair()
    data = b"release-payload"
    sig = priv.sign(data)
    assert signing.verify_signature(data, sig, pub_pem) is True


def test_verify_signature_rejects_tampered_payload():
    priv, pub_pem = _fresh_keypair()
    sig = priv.sign(b"original")
    assert signing.verify_signature(b"tampered", sig, pub_pem) is False


def test_verify_signature_rejects_wrong_key():
    _, pub_pem = _fresh_keypair()
    other_priv, _ = _fresh_keypair()
    sig = other_priv.sign(b"x")
    assert signing.verify_signature(b"x", sig, pub_pem) is False
```

Run: `python -m pytest tests/test_agent_signing.py -q` → expect FAIL (`ModuleNotFoundError: agent.signing`).

- [ ] **Step 3: implement `agent/signing.py`**

```python
"""Hash + ed25519 signature verification used by the agent's auto-update.

The matching private key lives ONLY in the GHA release secret. The public
key is committed at agent/release_pubkey.pem and bundled by PyInstaller.
"""
from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_signature(data: bytes, sig: bytes, pubkey_pem: bytes) -> bool:
    """True iff `sig` is a valid ed25519 signature of `data` under `pubkey_pem`.

    Any structural failure (wrong key type, malformed PEM, bad signature)
    returns False — callers should treat False as "do not apply this update".
    """
    try:
        key = serialization.load_pem_public_key(pubkey_pem)
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(sig, data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
```

Run: `python -m pytest tests/test_agent_signing.py -q` → expect 4 passed.

- [ ] **Step 4: `scripts/generate_release_keypair.py`** (one-off — generates the real release keypair). Do NOT run it during this task; just commit the script. (The maintainer runs it once, commits the public key, and stores the private key in GHA secrets.)

```python
"""Generate the agent's release-signing keypair.

Run ONCE:
    python scripts/generate_release_keypair.py

Writes the public key to agent/release_pubkey.pem (commit it).
Prints the private key to stdout — copy it into the GitHub Actions repository
secret named AGENT_RELEASE_PRIVATE_KEY. DO NOT commit the private key.
"""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open("agent/release_pubkey.pem", "wb") as f:
        f.write(pub_pem)
    print("✅ Wrote agent/release_pubkey.pem — commit it.")
    print()
    print("Add this PRIVATE key to GHA secret AGENT_RELEASE_PRIVATE_KEY:")
    print("-" * 60)
    print(priv_pem.decode("ascii"))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: placeholder `agent/release_pubkey.pem`** — write a minimal but VALID ed25519 public key file (using a fresh keypair we generate inline in a quick Python REPL or by running the script once and committing only the public part). Concretely:

```bash
python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; from cryptography.hazmat.primitives import serialization; open('agent/release_pubkey.pem','wb').write(Ed25519PrivateKey.generate().public_key().public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo))"
```

(The matching private key is intentionally NOT preserved — this is a throwaway placeholder. The maintainer regenerates a real keypair via Step 4's script before the first real release; their public key replaces this placeholder.)

- [ ] **Step 6: commit**

```bash
git add agent/_version.py agent/signing.py agent/release_pubkey.pem scripts/generate_release_keypair.py tests/test_agent_signing.py
git commit -m "feat(agent): version + ed25519 signing primitives + keypair generator"
```

---

### Task 2: Server release endpoints

**Files:**
- Create: `core/release_store.py`
- Modify: `blueprints/agent.py`, `app.py`
- Test: `tests/test_release_routes.py`

- [ ] **Step 1: write `core/release_store.py`**

```python
"""Filesystem helpers for the agent release directory.

Releases live in /data/releases (bind-mounted from the host in the hosted
deploy; configurable via DLD_RELEASES_DIR for local/dev). The manifest is a
JSON file; binaries are named like `dld-agent-windows-0.1.0.exe`.

Path-traversal guard: only the manifest and binaries whose basename matches
[A-Za-z0-9._-]+ may be served.
"""
from __future__ import annotations

import os
import re

_NAME_OK = re.compile(r"^[A-Za-z0-9._-]+$")


def releases_dir() -> str:
    return os.environ.get("DLD_RELEASES_DIR", "/data/releases")


def manifest_path() -> str:
    return os.path.join(releases_dir(), "manifest.json")


def binary_path(filename: str) -> str | None:
    """Resolve a binary filename inside the releases dir, or None if invalid."""
    if not _NAME_OK.fullmatch(filename or ""):
        return None
    p = os.path.join(releases_dir(), filename)
    # Defense-in-depth: ensure the resolved path is still inside releases_dir.
    rd = os.path.realpath(releases_dir())
    rp = os.path.realpath(p)
    if not rp.startswith(rd + os.sep):
        return None
    return p
```

- [ ] **Step 2: write failing tests** `tests/test_release_routes.py`

```python
import json
import os

import pytest
from core import auth


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "true")
    monkeypatch.setenv("DLD_RELEASES_DIR", str(tmp_path / "releases"))
    os.makedirs(str(tmp_path / "releases"), exist_ok=True)
    import importlib
    import core.db as db, core.devices as devices, core.release_store as rs
    importlib.reload(db); importlib.reload(devices); importlib.reload(rs); db.init_db()
    auth.reset_lockouts(); auth.set_password("pw")
    import app as m; importlib.reload(m)
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c, tmp_path / "releases"


def test_manifest_returns_404_when_missing(client):
    c, _ = client
    resp = c.get("/agent/releases/manifest.json")
    assert resp.status_code == 404


def test_manifest_served_when_present(client):
    c, rdir = client
    (rdir / "manifest.json").write_text(json.dumps({"version": "0.2.0"}))
    resp = c.get("/agent/releases/manifest.json")
    assert resp.status_code == 200
    assert resp.get_json() == {"version": "0.2.0"}


def test_binary_served_when_present(client):
    c, rdir = client
    (rdir / "dld-agent-windows-0.2.0.exe").write_bytes(b"BINARY_BYTES")
    resp = c.get("/agent/releases/dld-agent-windows-0.2.0.exe")
    assert resp.status_code == 200
    assert resp.data == b"BINARY_BYTES"


def test_binary_path_traversal_rejected(client):
    c, _ = client
    resp = c.get("/agent/releases/..%2Fetc%2Fpasswd")
    # The route rejects the bad filename before touching the filesystem.
    assert resp.status_code in (400, 404)


def test_release_endpoints_404_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.delenv("HYBRID_AGENT_ENABLED", raising=False)
    import importlib
    import core.db as db, core.devices as devices
    importlib.reload(db); importlib.reload(devices); db.init_db()
    auth.set_password("pw")
    import app as m; importlib.reload(m)
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        resp = c.get("/agent/releases/manifest.json")
        assert resp.status_code == 404
```

Run: `python -m pytest tests/test_release_routes.py -q` → expect FAIL (routes don't exist).

- [ ] **Step 3: add routes to `blueprints/agent.py`**.

First, ensure `os` is imported at the top of the file (currently the module imports `secrets`, `json` etc. but may not import `os` — add it if absent), and extend the existing Flask import line to include `abort, send_file`:

```python
import os
...
from flask import Blueprint, abort, jsonify, request, send_file
```

Then append (after the existing pairing/socket code; uses `core.release_store`):

```python
from core import release_store as _release_store


@bp.route("/agent/releases/manifest.json", methods=["GET"])
def release_manifest():
    p = _release_store.manifest_path()
    if not os.path.isfile(p):
        abort(404)
    return send_file(p, mimetype="application/json")


@bp.route("/agent/releases/<filename>", methods=["GET"])
def release_binary(filename):
    p = _release_store.binary_path(filename)
    if p is None:
        abort(400)
    if not os.path.isfile(p):
        abort(404)
    return send_file(p, mimetype="application/octet-stream", as_attachment=True,
                     download_name=filename)
```

- [ ] **Step 4: extend `_PUBLIC_ENDPOINTS`** in `app.py` so the release endpoints are reachable without a session (binaries are signed; signature is the security boundary). The gated `if HYBRID_AGENT_ENABLED:` block already calls `_PUBLIC_ENDPOINTS.update(...)`. Add the new endpoint names:

```python
        _PUBLIC_ENDPOINTS.update({
            "agent.pair_redeem", "agent_socket",
            "agent.release_manifest", "agent.release_binary",
        })
```

- [ ] **Step 5:** run tests → 5 passed. Run full suite → all pass, 3 pre-existing live-cred skips.

- [ ] **Step 6: commit**

```bash
git add core/release_store.py blueprints/agent.py app.py tests/test_release_routes.py
git commit -m "feat(agent): server endpoints serving signed release manifest + binaries"
```

---

### Task 3: Agent updater (check + download + verify + decide)

**Files:**
- Create: `agent/updater.py`
- Modify: `agent/main.py` (call `updater.check_and_apply()` on startup)
- Test: `tests/test_agent_updater.py`

- [ ] **Step 1: write failing tests** `tests/test_agent_updater.py`

```python
import base64
import json
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
    priv, pub_pem = _keypair()
    manifest = _make_manifest("0.1.0", "x.exe", b"DATA", priv, "windows")
    monkeypatch.setattr(updater, "_fetch_manifest", lambda url: manifest)
    monkeypatch.setattr(updater, "_current_version", lambda: "0.1.0")
    monkeypatch.setattr(updater, "_current_platform", lambda: "windows")
    assert updater.check_for_update("https://server.example") is None


def test_check_returns_build_when_newer(monkeypatch):
    priv, pub_pem = _keypair()
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
```

Run: expect FAIL (`ModuleNotFoundError: agent.updater`).

- [ ] **Step 2: implement `agent/updater.py`**

```python
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

Any failure logs and returns — the agent NEVER bricks itself trying to update.
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


def _fetch_manifest(server_url: str) -> dict:
    r = requests.get(server_url.rstrip("/") + "/agent/releases/manifest.json",
                     timeout=15)
    r.raise_for_status()
    return r.json()


def _fetch_bytes(url: str) -> bytes:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
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
```

Run: `python -m pytest tests/test_agent_updater.py -q` → expect 6 passed.

- [ ] **Step 3: wire into `agent/main.py`** — insert the update check right after `_ensure_paired` and before the `while True:` reconnect loop in `run()`. The current shape is:

```python
def run(server_url: str) -> None:
    token = _ensure_paired(server_url)
    while True:
        conn = AgentConnection(server_url, token)
        ...
```

Add the two highlighted lines and an import:

```python
from agent import updater  # at top of file with other agent imports
...
def run(server_url: str) -> None:
    token = _ensure_paired(server_url)
    try:
        updater.check_and_apply(server_url)  # NEW
    except Exception:                         # NEW
        log.debug("update check raised; continuing", exc_info=True)
    while True:
        conn = AgentConnection(server_url, token)
        ...  # rest unchanged
```

(If `updater.check_and_apply` finds and applies an update, it `os._exit(0)`s after spawning the new binary — control never returns to this function.)

Run the full suite: `python -m pytest -q` → all pass.

- [ ] **Step 4: commit**

```bash
git add agent/updater.py agent/main.py tests/test_agent_updater.py
git commit -m "feat(agent): self-update — manifest check, signature verify, OS-specific swap"
```

---

### Task 4: PyInstaller spec + local build script

**Files:**
- Create: `agent.spec`, `scripts/build_agent.py`

- [ ] **Step 1: `agent.spec`** — a PyInstaller spec file that builds a single-file bundle including `core/file_scanner.py` and the public-key file:

```python
# PyInstaller spec for the agent. Run via: pyinstaller agent.spec
# Produces dist/dld-agent (or dld-agent.exe on Windows).
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ['agent/main.py'],
    pathex=['.'],
    binaries=[],
    datas=[('agent/release_pubkey.pem', 'agent')],
    hiddenimports=['core.file_scanner', 'keyring.backends.Windows', 'keyring.backends.macOS'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['playwright', 'flask', 'flask_sock', 'openpyxl'],  # server-side, not needed
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='dld-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,  # phase 2b: keeps the pairing prompt visible
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
```

- [ ] **Step 2: `scripts/build_agent.py`** — runs PyInstaller, computes sha256, signs the artifact, emits a per-OS manifest fragment to stdout:

```python
"""Local build wrapper for the agent.

Usage (after generating the release keypair and storing the private key in
AGENT_RELEASE_PRIVATE_KEY env var or a file via --key-file):

    python scripts/build_agent.py --version 0.2.0

Runs PyInstaller, signs the bundle, and prints a JSON fragment suitable for
inclusion in manifest.json:
    {"sha256": "...", "signature_b64": "...", "filename": "..."}

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


def _binary_name(version: str) -> str:
    ext = ".exe" if _platform_label() == "windows" else ""
    return f"dld-agent-{_platform_label()}-{version}{ext}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="semver-ish, e.g. 0.2.0")
    ap.add_argument("--key-file", help="path to ed25519 private key PEM (else AGENT_RELEASE_PRIVATE_KEY env)")
    args = ap.parse_args()

    # Run PyInstaller.
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--clean",
                           "--noconfirm", "agent.spec"])
    built = os.path.join("dist", "dld-agent.exe" if _platform_label() == "windows" else "dld-agent")
    final_name = _binary_name(args.version)
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
        "platform": _platform_label(),
        "filename": final_name,
        "sha256": digest,
        "signature_b64": base64.b64encode(sig).decode("ascii"),
    }, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: commit** (no tests for the build script itself; PyInstaller-running tests are unreliable in CI):

```bash
git add agent.spec scripts/build_agent.py
git commit -m "build(agent): PyInstaller spec + local build/sign script"
```

---

### Task 5: GHA release workflow + VPS bind mount + runbook

**Files:**
- Create: `.github/workflows/release-agent.yml`, `docs/release-runbook.md`
- Modify: `deploy/docker-compose.yml`

- [ ] **Step 1: GHA workflow** `.github/workflows/release-agent.yml`

```yaml
name: release-agent
on:
  push:
    tags:
      - 'agent-v*'

jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        os: [windows-latest, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - name: Install build deps
        run: |
          python -m pip install --upgrade pip
          pip install -r agent/requirements.txt
          pip install pyinstaller
      - name: Extract version from tag
        id: ver
        shell: bash
        run: echo "version=${GITHUB_REF_NAME#agent-v}" >> "$GITHUB_OUTPUT"
      - name: Build + sign
        env:
          AGENT_RELEASE_PRIVATE_KEY: ${{ secrets.AGENT_RELEASE_PRIVATE_KEY }}
        shell: bash
        run: python scripts/build_agent.py --version "${{ steps.ver.outputs.version }}" > build-info.json
      - uses: actions/upload-artifact@v4
        with:
          name: agent-${{ matrix.os }}
          path: |
            dist/dld-agent-*
            build-info.json

  publish:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with: { path: artifacts }
      - name: Assemble manifest.json
        run: |
          python - <<'PY'
          import json, os, glob
          builds = {}
          for d in glob.glob("artifacts/agent-*"):
              info = json.load(open(os.path.join(d, "build-info.json")))
              info["url"] = f"https://autoalert.pro/agent/releases/{info['filename']}"
              builds[info["platform"]] = info
          version = os.environ["GITHUB_REF_NAME"].removeprefix("agent-v")
          manifest = {"version": version, "builds": builds}
          os.makedirs("release-payload", exist_ok=True)
          for d in glob.glob("artifacts/agent-*"):
              for b in glob.glob(os.path.join(d, "dld-agent-*")):
                  os.rename(b, os.path.join("release-payload", os.path.basename(b)))
          with open("release-payload/manifest.json", "w") as f:
              json.dump(manifest, f, indent=2)
          PY
      - name: SCP to VPS
        env:
          VPS_SSH_KEY: ${{ secrets.VPS_SSH_KEY }}
        run: |
          mkdir -p ~/.ssh && chmod 700 ~/.ssh
          echo "$VPS_SSH_KEY" > ~/.ssh/id_ed25519 && chmod 600 ~/.ssh/id_ed25519
          ssh-keyscan -H ${{ secrets.VPS_HOST }} >> ~/.ssh/known_hosts
          scp -i ~/.ssh/id_ed25519 release-payload/* ${{ secrets.VPS_USER }}@${{ secrets.VPS_HOST }}:~/dld-releases/
```

- [ ] **Step 2: VPS bind mount.** Edit `deploy/docker-compose.yml` and add to the `dld` service `volumes:` list:

```yaml
      - ~/dld-releases:/data/releases:ro
```

- [ ] **Step 3: runbook** `docs/release-runbook.md` (concise; the maintainer reads this once):

```markdown
# Cutting an Agent Release

## One-time setup (do once, ever)

1. Generate the release keypair locally:
   ```
   python scripts/generate_release_keypair.py
   ```
   - Commit `agent/release_pubkey.pem` (it will overwrite the placeholder).
   - Copy the printed private key.
2. In GitHub repo → Settings → Secrets and variables → Actions, add:
   - `AGENT_RELEASE_PRIVATE_KEY` — the private key PEM from step 1.
   - `VPS_SSH_KEY` — an ed25519 private key whose public half you've added to
     `~/.ssh/authorized_keys` on the VPS user (e.g. for the `dropshippa`
     user).
   - `VPS_HOST` — e.g. `autoalert.pro`.
   - `VPS_USER` — e.g. `dropshippa`.
3. On the VPS, create the release dir:
   ```
   mkdir -p ~/dld-releases
   ```
   (The dld container will bind-mount this at `/data/releases` read-only.)
   Redeploy once (`cd ~/DailyLifeDistributor/deploy && docker compose up -d`).

## Cutting a release

1. Bump `agent/_version.py` to the new version (e.g. `"0.2.0"`).
2. Commit: `chore(agent): bump version 0.2.0`.
3. Tag and push:
   ```
   git tag agent-v0.2.0
   git push --tags
   ```
4. The `release-agent` GHA workflow builds Windows + macOS, signs each, and
   SCPs the binaries + `manifest.json` to `~/dld-releases/` on the VPS.
5. Every running agent picks up the update on its next startup (or sooner if
   we add relay-pushed update notifications later).

## Rotating the signing key

Run `scripts/generate_release_keypair.py` again, replace the GHA secret +
committed public key, and cut a new release. **Agents running an OLDER public
key will reject the new builds and stay on their current version.** Plan
key rotations to coincide with handoff events; never silently rotate.
```

- [ ] **Step 4: commit**

```bash
git add .github/workflows/release-agent.yml deploy/docker-compose.yml docs/release-runbook.md
git commit -m "ci(agent): tag-triggered GHA matrix release + VPS bind mount + runbook"
```

---

## Phase 2b Acceptance

Once Tasks 1-5 are merged and the runbook's one-time setup is done:
- Pushing `git tag agent-vX.Y.Z && git push --tags` builds, signs, and uploads Windows + macOS binaries + a signed `manifest.json` to the VPS.
- Every running agent fetches the manifest on next startup; if the version is newer, downloads the right platform's binary, verifies sha256 + ed25519 signature against the baked public key, and self-applies (Windows rename pattern, macOS overwrite + `xattr -d com.apple.quarantine`).
- Failure modes never brick the agent (download fail / signature fail / swap fail → log + keep running the current version).
- From-source runs (`python -m agent.main`) skip updates entirely so dev work is undisturbed.

## Deferred (later, not this plan)
- Relay-pushed `update_available` notification (currently startup-check only — fine for the daily-restart cadence of this app).
- Per-platform code-signing certs (Apple Developer, Windows Authenticode) for seamless first-install on macOS Gatekeeper and Windows SmartScreen.
- Linux build (one row in the matrix when needed).
- An auto-update setting / "check now" button in the dashboard.
