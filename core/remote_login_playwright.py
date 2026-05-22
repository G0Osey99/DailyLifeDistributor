"""Real Playwright-backed browser handle for RemoteLoginManager.

Runs the sync Playwright API on a dedicated thread (sync objects are thread-
bound) and exposes goto/storage_state/close that marshal onto that thread.
On the VPS, Chrome is launched headed inside Xvfb (DISPLAY must be set, e.g.
':99'); locally with a real display it just opens a window.
"""
from __future__ import annotations

import logging
import os
import queue
import threading

from core.playwright_session import SessionConfig

_log = logging.getLogger(__name__)


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
                except Exception as e:  # noqa: BLE001 — teardown is best-effort
                    _log.debug("remote-login playwright teardown failed: %s", e)


def default_browser_launcher(config: SessionConfig) -> PlaywrightBrowserHandle:
    return PlaywrightBrowserHandle(config)
