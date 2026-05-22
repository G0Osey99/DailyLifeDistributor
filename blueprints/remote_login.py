"""Authenticated control routes for the remote-login browser.

Routes (all behind the global auth gate): start, status, save, cancel.
A module-level RemoteLoginManager holds the single live browser. The browser
launcher defaults to the real Playwright one but is swappable for tests.
"""
from __future__ import annotations

import logging
import threading
import time

from flask import Blueprint, jsonify, request

from core import remote_login, vnc
from core.hosted import is_hosted
from core.playwright_session import SessionConfig

bp = Blueprint("remote_login", __name__)
_log = logging.getLogger(__name__)


# Per-service login configs. Reuse each uploader's SessionConfig so login URLs
# / markers stay in one place.
def _service_configs() -> dict[str, SessionConfig]:
    from dataclasses import replace
    from uploaders.simplecast_uploader import _SC_SESSION_CONFIG_BASE, _resolve_upload_url
    from uploaders.vista_social_uploader import _VS_SESSION_CONFIG
    from uploaders.rock.client import _ROCK_SESSION_CONFIG
    # SimpleCast's base config leaves target_url/login_url empty (resolved
    # per-upload-call), so fill it here or the login browser navigates to "".
    # Navigating to the show's new-episode URL bounces an anonymous session to
    # the SimpleCast sign-in page — exactly the login we want to capture.
    return {
        "simplecast": replace(_SC_SESSION_CONFIG_BASE, target_url=_resolve_upload_url()),
        "vista_social": _VS_SESSION_CONFIG,
        "rock": _ROCK_SESSION_CONFIG,
    }


def _default_launcher(config):
    from core.remote_login_playwright import default_browser_launcher
    return default_browser_launcher(config)


# Single live manager for the process. On teardown (cancel / save / idle
# timeout) the per-session VNC server is stopped so its one-time password dies
# with the session.
manager = remote_login.RemoteLoginManager(
    browser_launcher=_default_launcher,
    on_teardown=vnc.stop_session,
)


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
    # Bring up a fresh single-use VNC password for this session. If it fails,
    # don't leave a half-started login around.
    try:
        vnc.start_session()
    except Exception as e:  # noqa: BLE001
        manager.cancel()
        return jsonify({"ok": False, "error": f"could not start VNC: {e}"}), 500
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
    # The Settings panel polls this every 2s while it's open, which is our only
    # signal that the operator is still present (the VNC traffic never touches
    # the app). Treat a poll as activity so an actively-watched session isn't
    # reaped; the idle reaper only fires once polling stops (tab closed).
    manager.touch()
    manager.poll_timeout()
    return jsonify(_status_dict())


def _status_dict() -> dict:
    st = manager.status()
    # Per-session VNC password gates the noVNC stream (Caddy can't forward_auth
    # a WS upgrade). These routes are all behind the app auth gate, so only an
    # authenticated operator ever receives it; it's regenerated each session and
    # cleared on teardown. Empty locally (no hosted stack).
    return {
        "active": st.active,
        "service": st.service,
        "phase": st.phase,
        "message": st.message,
        "vnc_password": vnc.current_password(),
    }


# ── Background idle reaper (option 3) ───────────────────────────────────────
# status() only runs poll_timeout() while a browser tab is polling; if the
# operator closes the tab, an abandoned session would linger. A daemon tick
# enforces the manager's idle timeout regardless. Hosted-only so local/test
# runs don't spawn a stray thread.
_reaper_started = False


def _start_idle_reaper(interval_s: int = 30) -> None:
    global _reaper_started
    if _reaper_started or not is_hosted():
        return
    _reaper_started = True

    def _loop():
        while True:
            time.sleep(interval_s)
            try:
                manager.poll_timeout()
            except Exception as e:  # noqa: BLE001 — reaper must keep looping
                _log.debug("remote-login reaper poll_timeout failed: %s", e)
            # Also sweep abandoned media-upload temp dirs (Task 8): any run
            # dir whose run is no longer active gets removed, bounding disk.
            try:
                from blueprints.media import active_run_ids
                from core import media_session as _ms
                _ms.sweep_orphans(active_run_ids())
            except Exception as e:  # noqa: BLE001 — reaper must keep looping
                _log.debug("media orphan sweep failed: %s", e)

    threading.Thread(target=_loop, name="remote-login-reaper", daemon=True).start()


_start_idle_reaper()
