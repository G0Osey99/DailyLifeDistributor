"""Stable user-facing agent download URLs (phase δ).

These wrap the existing ``/agent/releases/...`` endpoints so installer docs
and email links can hard-code ``/download/agent/windows`` and
``/download/agent/macos`` even if the release storage layout changes.

The landing page ``/download/agent`` auto-detects the visitor's OS from the
User-Agent and highlights the matching button. When the user is logged in,
the page also embeds a one-time pairing code (TTL 30 minutes) so the
install → paste-code → done sequence is one continuous flow (phase δ
task 10 wires the pairing code in).

Routes are session-gated through the global ``before_request`` hook in
``app.py`` (same as the rest of the dashboard). Anonymous visitors get
redirected to ``/login``.
"""
from __future__ import annotations

import json
import os

from flask import (
    Blueprint, redirect, render_template, request,
    session as flask_session, url_for,
)

from core import devices, release_store

bp = Blueprint("download", __name__)

# Stable fallback filenames used when the manifest is absent or doesn't list
# a build for the requested platform. They point at the same /agent/releases/
# namespace the agent auto-updater already uses, so a missing release is a
# clean 404 from release_binary rather than a 500 from this blueprint.
_FALLBACK_BINARY = {
    "windows": "dld-agent-windows.exe",
    # We ship a single universal2 macOS binary that runs on both Apple
    # Silicon and Intel — see .github/workflows/release-agent.yml for the
    # build recipe. The -arm64 / -intel keys are kept for the legacy
    # routes (so an old email or bookmark still works); both point at the
    # same universal binary.
    "macos": "dld-agent-macos",
    "macos-arm64": "dld-agent-macos",
    "macos-intel": "dld-agent-macos",
}


def _detect_os(user_agent: str) -> str:
    """Best-effort OS detection from User-Agent. Falls back to ``"other"``."""
    ua = (user_agent or "").lower()
    if "windows" in ua:
        return "windows"
    if "mac os" in ua or "macintosh" in ua or "darwin" in ua:
        return "macos"
    return "other"


def _resolve_binary(platform: str) -> str:
    """Return the filename to redirect to for ``platform``.

    Reads the releases manifest if present; falls back to a stable filename
    so a missing manifest doesn't break the user-facing route. The agent
    auto-updater's manifest schema looks like
    ``{"version": "0.6.0", "builds": {"windows": {"url": ".../foo.exe", ...},
    "macos": {...}}}``.
    """
    manifest_p = release_store.manifest_path()
    if os.path.isfile(manifest_p):
        try:
            with open(manifest_p, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
            build = (manifest.get("builds") or {}).get(platform) or {}
            url = build.get("url") or ""
            # The url may be an absolute https:// or a relative path. Either
            # way, the *filename* (basename) is what release_binary will
            # serve via /agent/releases/<filename>.
            if url:
                filename = url.rstrip("/").rsplit("/", 1)[-1]
                if filename:
                    return filename
        except (OSError, ValueError, KeyError):
            pass
    return _FALLBACK_BINARY.get(platform, _FALLBACK_BINARY["windows"])


@bp.route("/download/agent", methods=["GET"])
def landing():
    """OS-detection landing page with both download buttons.

    When the visitor is authenticated, a one-time pairing code (30-minute
    TTL) is minted and embedded in the page so the install →
    paste-code → done flow is one continuous sequence. The code is bound
    to ``flask.session['user_id']`` via ``create_pairing_code``'s
    ``user_id=`` kwarg; the agent's redeem call inherits that user_id.
    """
    detected = _detect_os(request.headers.get("User-Agent", ""))
    pairing_code = None
    try:
        uid = flask_session.get("user_id")
        if uid is not None:
            pairing_code = devices.create_pairing_code(
                ttl_seconds=1800,  # 30 minutes
                user_id=int(uid),
            )
    except Exception:  # noqa: BLE001 — a mint failure mustn't break the page
        pairing_code = None
    return render_template(
        "download_agent.html",
        detected_os=detected,
        windows_url=url_for("download.windows"),
        macos_url=url_for("download.macos"),
        pairing_code=pairing_code,
    )


@bp.route("/download/agent/windows", methods=["GET"])
def windows():
    """302 to the current Windows binary under /agent/releases/."""
    filename = _resolve_binary("windows")
    return redirect(f"/agent/releases/{filename}", code=302)


@bp.route("/download/agent/macos-arm64", methods=["GET"])
def macos_arm64():
    """302 to the current Apple Silicon (arm64) Mac binary."""
    filename = _resolve_binary("macos-arm64")
    return redirect(f"/agent/releases/{filename}", code=302)


@bp.route("/download/agent/macos-intel", methods=["GET"])
def macos_intel():
    """302 to the current Intel Mac binary."""
    filename = _resolve_binary("macos-intel")
    return redirect(f"/agent/releases/{filename}", code=302)


@bp.route("/download/agent/macos", methods=["GET"])
def macos():
    """302 to the current macOS binary under /agent/releases/."""
    filename = _resolve_binary("macos")
    return redirect(f"/agent/releases/{filename}", code=302)
