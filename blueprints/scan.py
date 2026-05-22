"""Index (Setup) page.

The browser-streaming pipeline drives everything else from the dashboard via
the /media/* endpoints (folder pickers, spreadsheet upload, scan, chunked
batch upload). The old server-side directory browse/validate/scan routes were
removed — media lives on the user's machine now.
"""
from __future__ import annotations

from flask import Blueprint, render_template

from core import db as _db
from core.config import load_config
from uploaders.youtube_uploader import is_authenticated as yt_is_authenticated

bp = Blueprint("scan", __name__)


@bp.route("/")
def index():
    config = load_config()
    return render_template(
        "index.html",
        platforms=config.get("platforms", {}),
        youtube_authenticated=yt_is_authenticated(),
        resume_session=_db.get_latest_in_progress(),
    )
