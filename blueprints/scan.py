"""Index (Setup) page.

The browser-streaming pipeline drives everything else from the dashboard via
the /media/* endpoints (folder pickers, spreadsheet upload, scan, chunked
batch upload). The old server-side directory browse/validate/scan routes were
removed — media lives on the user's machine now.
"""
from __future__ import annotations

from flask import Blueprint, render_template, session as flask_session

from core.config import load_config
from uploaders.youtube_uploader import is_authenticated as yt_is_authenticated

bp = Blueprint("scan", __name__)


@bp.route("/")
def index():
    config = load_config()
    # Phase δ: empty-state agent-download card shows when the current
    # user has zero (non-revoked) paired devices. New tenants land on
    # the dashboard with the agent install CTA front-and-center.
    show_card = False
    try:
        uid = flask_session.get("user_id")
        if uid is not None:
            from core import devices as _devices
            show_card = _devices.count_user_devices(int(uid)) == 0
    except Exception:  # noqa: BLE001 — never let a count crash the dashboard
        show_card = False
    return render_template(
        "index.html",
        platforms=config.get("platforms", {}),
        youtube_authenticated=yt_is_authenticated(),
        show_agent_download_card=show_card,
    )
