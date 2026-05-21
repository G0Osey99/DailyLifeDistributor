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
    import os
    st = manager.status()
    # The VNC password gates the noVNC stream (Caddy can't forward_auth a WS
    # upgrade). These routes are all behind the app auth gate, so only an
    # authenticated operator ever receives it. Empty locally (no hosted stack).
    return {
        "active": st.active,
        "service": st.service,
        "phase": st.phase,
        "message": st.message,
        "vnc_password": os.environ.get("VNC_PASSWORD", ""),
    }
