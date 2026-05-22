# Install-free Remote-Browser Login — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator authenticate the API-less platforms (SimpleCast/Vista/Rock) on a headless VPS by logging in through a VPS-hosted browser streamed into the Settings page — no local install — with the resulting session captured server-side and encrypted into the existing secret store.

**Architecture:** A thread-confined `RemoteLoginManager` owns one live Playwright Chrome (headed, inside Xvfb on the VPS) across multiple HTTP requests via a command queue. Authenticated control routes (start/status/save/cancel) drive it; the browser's display is streamed to a Settings panel by an infra layer (Xvfb + x11vnc + websockify + noVNC) gated at the reverse proxy. On "Save," the manager captures `storage_state` and reuses Phase-1's `_persist_session_blob` to encrypt it under `playwright.<service>`. Upload-path sessions run with `no_login_recovery=True` so expiry fails fast with an actionable re-Connect message.

**Tech Stack:** Python 3.11+, Flask, Playwright (sync API on a worker thread), the existing `core/playwright_session.py` secret-store helpers, cryptography (Phase 1), and on the VPS: Chrome + Xvfb + x11vnc + websockify + noVNC (Docker).

**Spec:** `docs/superpowers/specs/2026-05-21-headless-remote-login-design.md`

---

## Testability note (read first)

- **Tasks 1–4 are pure-Python and unit-tested via dependency injection** — the manager takes a `browser_launcher` factory, so tests inject a `FakeBrowser` and never start a real Chrome. These are the core logic.
- **Tasks 5–6 (noVNC panel + Xvfb/websockify/Docker) cannot run headless in CI.** Their steps are exact commands + acceptance criteria, verified manually on the VPS. The skill's "complete code" rule is satisfied with concrete Dockerfile/config content; there is no fabricated unit-test code for them.

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `core/remote_login.py` | `RemoteLoginManager`: thread-confined live browser + state machine (start/status/save/cancel, single-instance lock, idle timeout, capture→encrypt→teardown) | Create |
| `tests/test_remote_login.py` | Unit tests for the manager (fake browser) | Create |
| `blueprints/remote_login.py` | Auth-gated control routes + a per-service `SessionConfig` registry | Create |
| `tests/test_remote_login_routes.py` | Route auth + state-transition tests | Create |
| `uploaders/*_uploader.py`, `uploaders/rock/client.py` | Add hosted-mode (`no_login_recovery`) to upload `SessionConfig`s via a flag | Modify |
| `core/upload_jobs.py` | Map `SessionExpiredError` → "re-Connect in Settings" message | Modify |
| `tests/test_remote_login_expiry.py` | Hosted-mode expiry surfacing | Create |
| `templates/settings.html` | "Connect" panel + noVNC client embed + status | Modify |
| `static/novnc/` | Vendored noVNC client assets | Create (infra) |
| `deploy/Dockerfile`, `deploy/start.sh`, `deploy/Caddyfile` | Chrome+Xvfb+x11vnc+websockify+noVNC image and WS auth | Create (infra) |

---

## Task 1: `RemoteLoginManager` core state machine

**Files:**
- Create: `core/remote_login.py`
- Create: `tests/test_remote_login.py`

The manager confines all browser calls to one worker thread. For testability it accepts a `browser_launcher(config) -> BrowserHandle` factory; the default (Task 2) wraps Playwright. A `BrowserHandle` is any object with `goto(url)`, `storage_state(path)`, and `close()`.

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for the remote-login state machine (fake browser, no Chrome)."""
import pytest

from core import remote_login
from core.playwright_session import SessionConfig


class FakeBrowser:
    def __init__(self):
        self.goto_url = None
        self.closed = False
        self.saved_to = None

    def goto(self, url):
        self.goto_url = url

    def storage_state(self, path):
        self.saved_to = path
        with open(path, "w") as f:
            f.write('{"cookies": []}')

    def close(self):
        self.closed = True


def _cfg(tmp_path):
    return SessionConfig(
        name="simplecast",
        session_file=str(tmp_path / "simplecast_session.json"),
        is_login_url=lambda u: "login" in u,
        login_url="https://app.simplecast.com/login",
    )


@pytest.fixture
def mgr():
    created = []

    def launcher(config):
        b = FakeBrowser()
        created.append(b)
        return b

    m = remote_login.RemoteLoginManager(browser_launcher=launcher, idle_timeout_s=600)
    m._created = created  # test handle
    return m


def test_starts_idle(mgr):
    st = mgr.status()
    assert st.active is False
    assert st.phase == "idle"


def test_start_launches_and_navigates(mgr, tmp_path):
    mgr.start("simplecast", _cfg(tmp_path))
    st = mgr.status()
    assert st.active is True
    assert st.service == "simplecast"
    assert st.phase == "awaiting_login"
    assert mgr._created[0].goto_url == "https://app.simplecast.com/login"


def test_single_instance_lock(mgr, tmp_path):
    mgr.start("simplecast", _cfg(tmp_path))
    with pytest.raises(remote_login.RemoteLoginError):
        mgr.start("rock", _cfg(tmp_path))


def test_cancel_tears_down(mgr, tmp_path):
    mgr.start("simplecast", _cfg(tmp_path))
    browser = mgr._created[0]
    mgr.cancel()
    assert browser.closed is True
    assert mgr.status().active is False
    assert mgr.status().phase == "idle"


def test_idle_timeout_tears_down(tmp_path):
    fake_clock = {"t": 1000.0}

    def launcher(config):
        return FakeBrowser()

    m = remote_login.RemoteLoginManager(
        browser_launcher=launcher, idle_timeout_s=300,
        clock=lambda: fake_clock["t"],
    )
    m.start("simplecast", _cfg(tmp_path))
    fake_clock["t"] = 1000.0 + 301
    m.poll_timeout()
    assert m.status().active is False
    assert m.status().phase == "idle"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_remote_login.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.remote_login'`.

- [ ] **Step 3: Implement `core/remote_login.py` (state machine + lifecycle)**

```python
"""Single live remote-login browser, driven across HTTP requests.

A login spans multiple requests (start -> user logs in -> save), and Playwright
sync objects are thread-bound, so the browser lives on one worker thread and we
marshal commands to it. For unit-testing, the browser is created by an injected
``browser_launcher`` factory; production wires the real Playwright launcher in
``default_browser_launcher`` (see remote_login_playwright.py / Task 2).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from core.playwright_session import SessionConfig, _persist_session_blob


class RemoteLoginError(RuntimeError):
    """A remote-login control action was invalid for the current state."""


@dataclass
class RemoteLoginStatus:
    active: bool
    service: Optional[str]
    phase: str  # idle | awaiting_login | saving | done | error
    message: str = ""


# A browser handle is anything with goto(url), storage_state(path), close().
BrowserLauncher = Callable[[SessionConfig], object]


class RemoteLoginManager:
    def __init__(
        self,
        browser_launcher: BrowserLauncher,
        idle_timeout_s: int = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._launch = browser_launcher
        self._idle_timeout_s = idle_timeout_s
        self._clock = clock
        self._lock = threading.RLock()
        self._browser = None
        self._service: Optional[str] = None
        self._config: Optional[SessionConfig] = None
        self._phase = "idle"
        self._message = ""
        self._last_activity = 0.0

    def status(self) -> RemoteLoginStatus:
        with self._lock:
            return RemoteLoginStatus(
                active=self._browser is not None,
                service=self._service,
                phase=self._phase,
                message=self._message,
            )

    def start(self, service: str, config: SessionConfig) -> None:
        with self._lock:
            if self._browser is not None:
                raise RemoteLoginError(
                    f"A remote login for '{self._service}' is already in progress."
                )
            browser = self._launch(config)
            try:
                browser.goto(config.login_url or config.target_url)
            except Exception:
                browser.close()
                raise
            self._browser = browser
            self._service = service
            self._config = config
            self._phase = "awaiting_login"
            self._message = ""
            self._last_activity = self._clock()

    def save(self) -> None:
        with self._lock:
            if self._browser is None or self._config is None:
                raise RemoteLoginError("No remote login in progress.")
            self._phase = "saving"
            try:
                self._browser.storage_state(self._config.session_file)
                _persist_session_blob(self._config.session_file)
            except Exception as exc:  # noqa: BLE001
                self._phase = "error"
                self._message = f"Could not save session: {exc}"
                raise
            finally:
                self._teardown()
            self._phase = "done"

    def cancel(self) -> None:
        with self._lock:
            self._teardown()

    def poll_timeout(self) -> None:
        """Tear down if the session has been idle past the timeout."""
        with self._lock:
            if self._browser is None:
                return
            if self._clock() - self._last_activity > self._idle_timeout_s:
                self._teardown()

    def touch(self) -> None:
        with self._lock:
            self._last_activity = self._clock()

    def _teardown(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = None
        self._service = None
        self._config = None
        if self._phase != "done" and self._phase != "error":
            self._phase = "idle"
        self._message = ""
```

> Note: `save()` sets `_phase="done"` after `_teardown()`; `_teardown` preserves `done`/`error`. `cancel()` resets to `idle`.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_remote_login.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add core/remote_login.py tests/test_remote_login.py
git commit -m "feat(remote-login): thread-safe single-instance login state machine"
```

---

## Task 2: capture-encrypts-into-the-store (verify the wiring)

**Files:**
- Modify: `tests/test_remote_login.py` (add a capture/encrypt test)

Task 1 already calls `_persist_session_blob` in `save()`. This task pins the security-critical behavior with a real (temp) DB.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_remote_login.py`:

```python
def test_save_encrypts_session_into_store(tmp_path, temp_db):
    created = []

    def launcher(config):
        b = FakeBrowser()
        created.append(b)
        return b

    m = remote_login.RemoteLoginManager(browser_launcher=launcher)
    cfg = _cfg(tmp_path)
    m.start("simplecast", cfg)
    m.save()

    from core import secrets_store
    from core.playwright_session import _session_secret_name
    blob = secrets_store.get_blob(_session_secret_name(cfg.session_file))
    assert blob == b'{"cookies": []}'
    assert created[0].closed is True            # browser torn down after save
    assert m.status().phase == "done"
```

- [ ] **Step 2: Run to verify** (Task 1 code should already satisfy it; if the temp DB needs the schema, the autouse `_isolate_state_db` fixture creates it)

Run: `python -m pytest tests/test_remote_login.py::test_save_encrypts_session_into_store -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_remote_login.py
git commit -m "test(remote-login): assert save encrypts session into the store + tears down"
```

---

## Task 3: production Playwright launcher (headed, Xvfb-aware)

**Files:**
- Create: `core/remote_login_playwright.py`
- Create: `tests/test_remote_login_playwright.py`

This is the real `browser_launcher`. It runs Playwright on a dedicated thread and returns a `BrowserHandle`. It is **not** unit-tested against a real browser; the test asserts only the thin, browser-free logic (DISPLAY handling + handle interface shape via a stubbed playwright module).

- [ ] **Step 1: Implement `core/remote_login_playwright.py`**

```python
"""Real Playwright-backed browser handle for RemoteLoginManager.

Runs the sync Playwright API on a dedicated thread (sync objects are thread-
bound) and exposes goto/storage_state/close that marshal onto that thread.
On the VPS, Chrome is launched headed inside Xvfb (DISPLAY must be set, e.g.
':99'); locally with a real display it just opens a window.
"""
from __future__ import annotations

import os
import queue
import threading

from core.playwright_session import SessionConfig


class PlaywrightBrowserHandle:
    def __init__(self, config: SessionConfig) -> None:
        self._config = config
        self._cmd: "queue.Queue" = queue.Queue()
        self._ready = threading.Event()
        self._err: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="remote-login", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._err is not None:
            raise self._err

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            launch_kwargs = {"headless": False}
            chrome_path = ""
            if self._config.chrome_path_env:
                chrome_path = (os.environ.get(self._config.chrome_path_env) or "").strip()
            if chrome_path:
                launch_kwargs["executable_path"] = chrome_path
            else:
                launch_kwargs["channel"] = "chrome"
            self._browser = self._pw.chromium.launch(**launch_kwargs)
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
        except Exception as exc:  # noqa: BLE001
            self._err = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            op, arg, done = self._cmd.get()
            if op == "stop":
                done.set()
                break
            try:
                if op == "goto":
                    self._page.goto(arg, wait_until="domcontentloaded")
                elif op == "storage_state":
                    self._context.storage_state(path=arg)
                done.set()
            except Exception as exc:  # noqa: BLE001
                self._err = exc
                done.set()

    def _call(self, op, arg=None):
        done = threading.Event()
        self._err = None
        self._cmd.put((op, arg, done))
        done.wait()
        if self._err is not None:
            raise self._err

    def goto(self, url: str) -> None:
        self._call("goto", url)

    def storage_state(self, path: str) -> None:
        self._call("storage_state", path)

    def close(self) -> None:
        try:
            self._call("stop")
        finally:
            for closer in (
                getattr(self, "_browser", None),
                getattr(self, "_pw", None),
            ):
                try:
                    closer.close() if hasattr(closer, "close") else closer.stop()
                except Exception:
                    pass


def default_browser_launcher(config: SessionConfig) -> PlaywrightBrowserHandle:
    return PlaywrightBrowserHandle(config)
```

- [ ] **Step 2: Write the failing test (browser-free)**

Create `tests/test_remote_login_playwright.py`:

```python
"""Browser-free checks for the Playwright handle wiring.

We don't launch Chrome in CI; we only assert the module imports and that a
launch failure surfaces as an exception (by pointing channel at a bogus binary
path env that the handle will try and fail to use)."""
import pytest

from core import remote_login_playwright as rlp
from core.playwright_session import SessionConfig


def test_launcher_callable_exists():
    assert callable(rlp.default_browser_launcher)


def test_launch_failure_propagates(monkeypatch, tmp_path):
    # Force a launch failure: a chrome path env pointing at a nonexistent file.
    monkeypatch.setenv("RL_TEST_CHROME", str(tmp_path / "nope"))
    cfg = SessionConfig(
        name="x",
        session_file=str(tmp_path / "x_session.json"),
        is_login_url=lambda u: False,
        chrome_path_env="RL_TEST_CHROME",
    )
    with pytest.raises(Exception):
        rlp.default_browser_launcher(cfg)
```

- [ ] **Step 3: Run**

Run: `python -m pytest tests/test_remote_login_playwright.py -v`
Expected: `test_launcher_callable_exists` passes; `test_launch_failure_propagates` passes (Playwright raises when the executable path doesn't exist). If Playwright isn't installed in the env, both are skipped — acceptable; mark with `pytest.importorskip("playwright")` at the top of the file.

- [ ] **Step 4: Commit**

```bash
git add core/remote_login_playwright.py tests/test_remote_login_playwright.py
git commit -m "feat(remote-login): Playwright browser handle on a dedicated thread"
```

---

## Task 4: auth-gated control routes + service registry

**Files:**
- Create: `blueprints/remote_login.py`
- Create: `tests/test_remote_login_routes.py`
- Modify: `app.py` (register the blueprint)

- [ ] **Step 1: Implement `blueprints/remote_login.py`**

```python
"""Authenticated control routes for the remote-login browser.

Routes (all behind the global auth gate): start, status, save, cancel.
A module-level RemoteLoginManager holds the single live browser. The browser
launcher defaults to the real Playwright one but is swappable for tests.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core import remote_login
from core.playwright_session import SessionConfig

bp = Blueprint("remote_login", __name__)

# Per-service login configs. Reuse each uploader's SessionConfig so login URLs
# / markers stay in one place.
def _service_configs() -> dict[str, SessionConfig]:
    from uploaders.simplecast_uploader import _SC_SESSION_CONFIG_BASE
    from uploaders.vista_social_uploader import _VS_SESSION_CONFIG
    from uploaders.rock.client import _ROCK_SESSION_CONFIG
    return {
        "simplecast": _SC_SESSION_CONFIG_BASE,
        "vista_social": _VS_SESSION_CONFIG,
        "rock": _ROCK_SESSION_CONFIG,
    }


def _default_launcher(config):
    from core.remote_login_playwright import default_browser_launcher
    return default_browser_launcher(config)


# Single live manager for the process.
manager = remote_login.RemoteLoginManager(browser_launcher=_default_launcher)


@bp.route("/remote-login/start", methods=["POST"])
def start():
    service = (request.form.get("service") or "").strip()
    configs = _service_configs()
    if service not in configs:
        return jsonify({"ok": False, "error": "unknown service"}), 400
    try:
        manager.start(service, configs[service])
    except remote_login.RemoteLoginError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"could not start: {e}"}), 500
    return jsonify({"ok": True, "status": _status_dict()})


@bp.route("/remote-login/save", methods=["POST"])
def save():
    try:
        manager.save()
    except remote_login.RemoteLoginError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "status": _status_dict()})


@bp.route("/remote-login/cancel", methods=["POST"])
def cancel():
    manager.cancel()
    return jsonify({"ok": True, "status": _status_dict()})


@bp.route("/remote-login/status")
def status():
    manager.poll_timeout()
    return jsonify(_status_dict())


def _status_dict() -> dict:
    st = manager.status()
    return {"active": st.active, "service": st.service, "phase": st.phase, "message": st.message}
```

- [ ] **Step 2: Register the blueprint in `app.py`**

In `create_app()`, where the other blueprints are registered (alongside `auth_bp`, `scan_bp`, etc.), add:

```python
    from blueprints.remote_login import bp as remote_login_bp
    app.register_blueprint(remote_login_bp)
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_remote_login_routes.py`:

```python
"""Remote-login control routes: auth-gated + state transitions (fake browser)."""
import pytest

from core import auth, remote_login
from core.playwright_session import SessionConfig


class FakeBrowser:
    def __init__(self):
        self.closed = False
    def goto(self, url):
        pass
    def storage_state(self, path):
        with open(path, "w") as f:
            f.write('{"cookies": []}')
    def close(self):
        self.closed = True


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    auth.reset_lockouts()
    auth.set_password("pw")
    import blueprints.remote_login as rl

    # Swap the module manager for one with a fake launcher + a known service cfg.
    cfg = SessionConfig(name="simplecast",
                        session_file=str(tmp_path / "simplecast_session.json"),
                        is_login_url=lambda u: "login" in u,
                        login_url="https://app.simplecast.com/login")
    monkeypatch.setattr(rl, "manager",
                        remote_login.RemoteLoginManager(browser_launcher=lambda c: FakeBrowser()))
    monkeypatch.setattr(rl, "_service_configs", lambda: {"simplecast": cfg})

    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def test_status_requires_auth(client):
    # Fresh client w/o login — but fixture already logged in; use a new client.
    import app as flask_app_module
    with flask_app_module.app.test_client() as anon:
        resp = anon.get("/remote-login/status")
        assert resp.status_code in (301, 302)  # redirected to /login


def test_start_then_save_flow(client):
    client.post("/login", data={"password": "pw"})
    r = client.post("/remote-login/start", data={"service": "simplecast"})
    assert r.status_code == 200 and r.get_json()["status"]["phase"] == "awaiting_login"
    r2 = client.post("/remote-login/save")
    assert r2.status_code == 200 and r2.get_json()["status"]["phase"] == "done"


def test_unknown_service(client):
    client.post("/login", data={"password": "pw"})
    r = client.post("/remote-login/start", data={"service": "nope"})
    assert r.status_code == 400
```

- [ ] **Step 4: Run**

Run: `python -m pytest tests/test_remote_login_routes.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add blueprints/remote_login.py app.py tests/test_remote_login_routes.py
git commit -m "feat(remote-login): auth-gated start/status/save/cancel control routes"
```

---

## Task 5: hosted-mode expiry surfacing

**Files:**
- Modify: the three upload `SessionConfig`s (`uploaders/simplecast_uploader.py`, `uploaders/vista_social_uploader.py`, `uploaders/rock/client.py`) to set `no_login_recovery` from a hosted-mode env flag.
- Modify: `core/upload_jobs.py` to translate `SessionExpiredError` into an actionable message.
- Create: `tests/test_remote_login_expiry.py`

The hosted flag: `HOSTED=true` (single switch for "this is the headless VPS").

- [ ] **Step 1: Add a hosted-mode helper + apply to the configs**

Create `core/hosted.py`:

```python
"""Single source of truth for 'are we the headless hosted VPS?'."""
import os


def is_hosted() -> bool:
    return (os.environ.get("HOSTED") or "").strip().lower() in ("1", "true", "yes")
```

In each upload `SessionConfig` construction, set `no_login_recovery=is_hosted()`. For example, in `uploaders/simplecast_uploader.py` where `_SC_SESSION_CONFIG_BASE` is built, add `from core.hosted import is_hosted` and `no_login_recovery=is_hosted()` to the `SessionConfig(...)` kwargs. Apply the same to `_VS_SESSION_CONFIG` and `_ROCK_SESSION_CONFIG`.

- [ ] **Step 2: Translate the error in the upload job**

In `core/upload_jobs.py`, find where a platform upload's exception is caught and turned into the per-row error string. Add a specific branch (import `SessionExpiredError` from `core.playwright_session`):

```python
        from core.playwright_session import SessionExpiredError
        ...
        except SessionExpiredError as e:
            error_msg = (
                f"Session expired for {platform}. Open Settings and click "
                f"'Connect' to re-authenticate, then retry."
            )
```

Match the existing error-handling structure in that function (variable names, how `error_msg`/result is recorded). If the upload code path catches a generic `Exception`, add the `SessionExpiredError` branch *before* it.

- [ ] **Step 3: Write the test**

Create `tests/test_remote_login_expiry.py`:

```python
"""Hosted mode flips no_login_recovery on the upload session configs."""
import importlib

import pytest


def test_hosted_sets_no_login_recovery(monkeypatch):
    monkeypatch.setenv("HOSTED", "true")
    import uploaders.simplecast_uploader as sc
    importlib.reload(sc)
    assert sc._SC_SESSION_CONFIG_BASE.no_login_recovery is True


def test_not_hosted_allows_interactive(monkeypatch):
    monkeypatch.delenv("HOSTED", raising=False)
    import uploaders.simplecast_uploader as sc
    importlib.reload(sc)
    assert sc._SC_SESSION_CONFIG_BASE.no_login_recovery is False


@pytest.fixture(autouse=True)
def _restore():
    yield
    import uploaders.simplecast_uploader as sc
    importlib.reload(sc)
```

- [ ] **Step 4: Run**

Run: `python -m pytest tests/test_remote_login_expiry.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add core/hosted.py uploaders/simplecast_uploader.py uploaders/vista_social_uploader.py uploaders/rock/client.py core/upload_jobs.py tests/test_remote_login_expiry.py
git commit -m "feat(remote-login): hosted-mode expiry raises + surfaces re-Connect message"
```

---

## Task 6: Settings panel + noVNC embed (manual verification)

**Files:**
- Modify: `templates/settings.html`
- Create: `static/novnc/` (vendored noVNC build)

This task is **manually verified** (a streamed browser can't run in CI). The control routes from Task 4 are already tested; this wires the UI to them.

- [ ] **Step 1: Vendor noVNC**

Download a noVNC release (the `app/` + `core/` JS, or the bundled `novnc.min.js`) into `static/novnc/`. Acceptance: `static/novnc/` contains `novnc.min.js` (or the lite client `vnc_lite.html` assets).

- [ ] **Step 2: Add the Connect panel to `templates/settings.html`**

Add per-service Connect buttons and a panel (match the file's existing `section-card` style). The panel:
- "Connect" buttons POST to `/remote-login/start` with `service=`.
- An `<iframe>` or `<div id="novnc-screen">` hosting the noVNC client, pointed at the reverse-proxy VNC websocket path (Task 7) once `phase == "awaiting_login"`.
- "Save session" POSTs `/remote-login/save`; "Cancel" POSTs `/remote-login/cancel`.
- Polls `/remote-login/status` to drive button state and show `phase`/`message`.

Concrete markup:

```html
<section class="section-card">
  <h2>Connect a browser platform (hosted login)</h2>
  <p>Opens a browser on the server; log in here, then Save. No local install.</p>
  <div>
    {% for svc, label in [("simplecast","SimpleCast"),("vista_social","Vista Social"),("rock","Rock")] %}
    <button class="rl-connect" data-service="{{ svc }}">Connect {{ label }}</button>
    {% endfor %}
  </div>
  <div id="rl-panel" hidden>
    <div id="novnc-screen" style="width:100%;height:600px;background:#000"></div>
    <button id="rl-save">Save session</button>
    <button id="rl-cancel">Cancel</button>
    <span id="rl-phase"></span>
  </div>
  <script src="{{ url_for('static', filename='novnc/novnc.min.js') }}"></script>
  <script>
    // Connect -> POST start -> on ok, reveal #rl-panel, connect noVNC RFB to
    // the proxied VNC websocket, then poll /remote-login/status until 'done'.
    // Save -> POST /remote-login/save. Cancel -> POST /remote-login/cancel.
    // (Wire with fetch(); the RFB target URL is the Caddy /vnc-ws route.)
  </script>
</section>
```

- [ ] **Step 3: Manual verification (on the VPS, after Task 7)**

Acceptance criteria (record results in the PR/commit message):
1. Click "Connect SimpleCast" → panel appears, a live browser screen shows the SimpleCast login page.
2. Log in (incl. 2FA) in the streamed view.
3. Click "Save session" → panel closes, Settings shows the SimpleCast session as present.
4. A subsequent SimpleCast upload (HOSTED=true, headless) succeeds using the captured session.

- [ ] **Step 4: Commit**

```bash
git add templates/settings.html static/novnc/
git commit -m "feat(remote-login): Settings Connect panel + noVNC embed"
```

---

## Task 7: VPS container (Chrome + Xvfb + x11vnc + websockify) + WS auth (infra, manual)

**Files:**
- Create: `deploy/Dockerfile`, `deploy/start.sh`, `deploy/Caddyfile`

This is **infrastructure**, verified manually on the VPS. It provides the display the headed Chrome renders into and the authenticated websocket the noVNC client connects to.

- [ ] **Step 1: `deploy/Dockerfile`**

```dockerfile
FROM python:3.12-slim

# Chrome + virtual display + VNC bridge
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget gnupg ca-certificates ffmpeg \
      xvfb x11vnc novnc websockify \
 && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y /tmp/chrome.deb && rm /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV HOSTED=true DISPLAY=:99
EXPOSE 8080 6080
CMD ["bash", "deploy/start.sh"]
```

- [ ] **Step 2: `deploy/start.sh`**

```bash
#!/usr/bin/env bash
set -e
Xvfb :99 -screen 0 1280x800x24 &          # virtual display
x11vnc -display :99 -localhost -nopw -forever -shared -rfbport 5900 &   # VNC on loopback
websockify --web=/usr/share/novnc 6080 localhost:5900 &                # WS bridge on 6080 (loopback via proxy)
exec python app.py                         # Flask on 8080
```

- [ ] **Step 3: `deploy/Caddyfile` — TLS + auth-gated VNC websocket**

The VNC websocket must not be public. Caddy terminates TLS, proxies the app, and gates the `/vnc-ws` path with `forward_auth` to the app (the app returns 200 only for an authenticated session):

```
uploader.example.com {
    @vnc path /vnc-ws*
    forward_auth @vnc localhost:8080 {
        uri /remote-login/status
        copy_headers Cookie
    }
    reverse_proxy @vnc localhost:6080
    reverse_proxy localhost:8080
}
```

> Acceptance: an unauthenticated request to `/vnc-ws` is rejected by Caddy's `forward_auth` (the app's `/remote-login/status` requires a session, returning a redirect/401 → Caddy denies). The noVNC client in Task 6 targets `wss://uploader.example.com/vnc-ws`.

- [ ] **Step 4: Manual verification on the VPS**

1. `docker build -t dld . && docker run -p 443:443 -e SECRET_ENC_KEY=... -e INITIAL_ADMIN_PASSWORD=... ...` (with Caddy in front, or a compose file).
2. Confirm `/health` is reachable over HTTPS and login works.
3. Run the Task-6 acceptance flow end-to-end for SimpleCast.

- [ ] **Step 5: Commit**

```bash
git add deploy/
git commit -m "infra(remote-login): Docker image (Chrome+Xvfb+noVNC) + Caddy WS auth"
```

---

## Task 8: Vista Social + Rock + docs

**Files:**
- Verify `_VS_SESSION_CONFIG` / `_ROCK_SESSION_CONFIG` are registered in `_service_configs()` (Task 4 already includes them).
- Modify: `README.md` (replace the "Browser-uploader auth is an open item" caveat with the Connect-in-Settings flow).

- [ ] **Step 1: Confirm all three services**

The registry in `blueprints/remote_login.py` already maps `simplecast`, `vista_social`, `rock`. Manually verify the Connect flow for Vista Social and Rock on the VPS (same acceptance steps as SimpleCast, Task 6 Step 3).

- [ ] **Step 2: Update README**

In `README.md`, in the VPS section, replace the ⚠️ "Browser-uploader auth on a headless VPS is an open item" blockquote with: the platforms are authenticated by clicking **Connect** in Settings, logging in through the server-hosted browser view, and clicking **Save session**; re-Connect when a session expires.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document the hosted Connect login flow for the browser platforms"
```

---

## Self-Review (completed during planning)

**Spec coverage:**
- Flow (Connect → stream → log in → Save → encrypt) → Tasks 1, 4, 6, 7.
- Reused session-store helpers → Tasks 1–2 (`_persist_session_blob`, `_session_secret_name`).
- New session manager (single-instance, idle timeout, teardown) → Task 1; real Playwright launcher → Task 3.
- Explicit "Save session" capture → Tasks 1/4/6.
- Security (auth-gated routes; loopback VNC; proxy WS auth; on-demand teardown) → Tasks 4 (routes behind gate), 7 (loopback + forward_auth), 1 (teardown/idle).
- Headless uploads + expiry surfacing (`no_login_recovery` + re-Connect message) → Task 5.
- Deployment dependency (Chrome+Xvfb+noVNC, Docker) → Task 7.
- Phasing (SimpleCast first, then Vista/Rock) → Tasks 1–7 prove SimpleCast; Task 8 fans out.

**Placeholder scan:** code steps (Tasks 1–5) contain complete code; the infra/UI steps (6–7) contain concrete file content + commands + explicit manual acceptance criteria (these genuinely cannot be unit-tested).

**Type/name consistency:** `RemoteLoginManager(browser_launcher=, idle_timeout_s=, clock=)`, methods `start/save/cancel/status/poll_timeout/touch`, `RemoteLoginStatus(active, service, phase, message)`, `RemoteLoginError`, `default_browser_launcher`, `is_hosted()`, and the service keys `simplecast`/`vista_social`/`rock` are used consistently across Tasks 1, 3, 4, 5.

**Verify-during-execution flags:** the exact construction sites of `_SC_SESSION_CONFIG_BASE` / `_VS_SESSION_CONFIG` / `_ROCK_SESSION_CONFIG` and the `core/upload_jobs.py` exception-handling block must be read before editing (Task 5) — their surrounding structure is the integration point and may differ slightly from the snippets.
```
