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
                # goto() completes before we release the lock and enter
                # awaiting_login; the UI's repeated status() polls during the login
                # wait are therefore lock-free.
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
            browser = self._browser
            config = self._config
            self._phase = "saving"
        # Blocking browser I/O runs outside the lock so status() polls during
        # the save don't block on storage_state()/encryption.
        try:
            browser.storage_state(config.session_file)
            _persist_session_blob(config.session_file)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._phase = "error"
                self._message = f"Could not save session: {exc}"
                self._teardown()
            raise
        with self._lock:
            self._phase = "done"
            self._teardown()

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
            if self._browser is not None:
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
        if self._phase not in ("done", "error"):
            self._phase = "idle"
            self._message = ""
