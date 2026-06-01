"""Bundled-Chromium management for the hybrid agent.

The agent no longer drives the user's system Google Chrome. Instead it
downloads Playwright's own Chromium once (on first run) into a writable,
update-surviving directory and launches that. Two problems this solves for
non-technical operators:

  * No dependency on Google Chrome being installed.
  * No macOS "<App> wants to control Chrome" Automation permission prompt —
    Playwright drives its own browser (a subprocess pipe / CDP), not
    Chrome.app, so macOS never asks for Automation/admin rights per run.

Why download-on-first-run rather than bundle the browser into the binary:
Chromium is per-arch (~150 MB) with no universal build, so bundling would
force either a ~500 MB universal binary or a per-arch build split (and the
Intel-Mac CI runner is now a paid SKU). Downloading keeps the agent binary
small and the macOS build a single universal2 — the browser lands in the
user's profile, picks the host arch automatically, and survives agent
self-updates (the binary is swapped; ``~/.dld-agent/browsers`` is not).

Coupling: ``core`` must not import ``agent``. So this module owns the
*decision* and merely sets two env vars; ``core.playwright_session.
chromium_launch_kwargs`` only reads them:

  * ``PLAYWRIGHT_BROWSERS_PATH`` — where Playwright looks for / installs the
    browser (our writable dir).
  * ``DLD_USE_BUNDLED_CHROMIUM`` — set to ``"1"`` only once a usable Chromium
    is confirmed present. Until then the launch falls back to the previous
    ``channel='chrome'`` behaviour, so a failed/blocked download degrades to
    "use system Chrome" rather than breaking uploads.
"""
from __future__ import annotations

import glob
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Serialise installs: the startup prewarm thread and the first run_batch can
# both call ensure_chromium() at once. The node driver install isn't safe to
# run twice into the same dir concurrently; the second caller waits, then sees
# the browser already present.
_install_lock = threading.Lock()


def browsers_dir() -> str:
    """Writable directory that holds the downloaded browser.

    Under ``~/.dld-agent`` (next to agent.json) so it survives binary
    self-updates — the updater swaps the executable / .app, never this dir.
    """
    d = os.path.join(os.path.expanduser("~"), ".dld-agent", "browsers")
    os.makedirs(d, exist_ok=True)
    return d


def is_installed(bdir: Optional[str] = None) -> bool:
    """True when a full Chromium (not just the headless shell) is present.

    Playwright lays the browser down as ``chromium-<rev>/`` alongside
    ``chromium_headless_shell-<rev>/``. We launch full Chromium, so match the
    hyphenated ``chromium-*`` dir specifically (the headless-shell dir starts
    with ``chromium_`` and won't match).
    """
    bdir = bdir or browsers_dir()
    return bool(glob.glob(os.path.join(bdir, "chromium-*")))


def _driver_argv() -> list[str]:
    """Resolve the Playwright node driver as an argv prefix for `install`.

    ``compute_driver_executable()`` returns ``(node, cli.js)`` on modern
    Playwright (1.58 here) and a single launcher path on older versions —
    handle both so a Playwright bump doesn't silently break installs.
    """
    from playwright._impl._driver import compute_driver_executable

    drv = compute_driver_executable()
    if isinstance(drv, (tuple, list)):
        return [str(p) for p in drv]
    return [str(drv)]


def prepare_env() -> str:
    """Point Playwright at our browsers dir; flag bundled mode if ready.

    Cheap (no download). Sets ``PLAYWRIGHT_BROWSERS_PATH`` always, and
    ``DLD_USE_BUNDLED_CHROMIUM`` only when a browser is already on disk from a
    previous run — so a launch before the first download still falls back to
    system Chrome instead of failing on a missing browser. Returns the dir.
    """
    bdir = browsers_dir()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bdir
    if is_installed(bdir):
        os.environ["DLD_USE_BUNDLED_CHROMIUM"] = "1"
    return bdir


def ensure_chromium(progress: Optional[Callable[[str], None]] = None) -> bool:
    """Guarantee a usable bundled Chromium, downloading it once if needed.

    Idempotent and best-effort: returns ``True`` when bundled Chromium is
    ready (and ``DLD_USE_BUNDLED_CHROMIUM`` is set), ``False`` if the download
    failed/was blocked — in which case the env flag is left unset so launches
    degrade to ``channel='chrome'`` (today's behaviour) rather than break.
    Never raises. ``progress`` receives short status/percentage lines.
    """
    bdir = prepare_env()
    if is_installed(bdir):
        os.environ["DLD_USE_BUNDLED_CHROMIUM"] = "1"
        return True

    with _install_lock:
        # Re-check under the lock: another thread may have just finished.
        if is_installed(bdir):
            os.environ["DLD_USE_BUNDLED_CHROMIUM"] = "1"
            return True
        if progress:
            progress("Downloading browser (one-time, ~150 MB)…")
        log.info("Downloading bundled Chromium into %s", bdir)
        try:
            ok = _install(bdir, progress)
        except Exception:
            log.exception("Bundled Chromium install raised; will use system Chrome")
            ok = False

    if ok and is_installed(bdir):
        os.environ["DLD_USE_BUNDLED_CHROMIUM"] = "1"
        if progress:
            progress("Browser ready.")
        log.info("Bundled Chromium ready in %s", bdir)
        return True

    log.warning(
        "Bundled Chromium not available; falling back to system Chrome "
        "(channel='chrome'). The agent will still upload if Google Chrome "
        "is installed."
    )
    return False


def _install(bdir: str, progress: Optional[Callable[[str], None]]) -> bool:
    """Run ``<driver> install chromium`` into *bdir*. Returns success."""
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=bdir)
    argv = _driver_argv() + ["install", "chromium"]
    proc = subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        log.debug("playwright install: %s", line)
        if progress and ("%" in line or "Downloading" in line or "Chromium" in line):
            progress(line)
    rc = proc.wait()
    if rc != 0:
        log.error("playwright install chromium exited rc=%s", rc)
    return rc == 0
