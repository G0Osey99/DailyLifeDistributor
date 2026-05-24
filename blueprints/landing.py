"""Public marketing landing page served at ``/``.

The dashboard now lives at ``/dashboard``; ``/`` is the unauthenticated entry
point with sign-in calls-to-action that link into the dashboard. The endpoint
is in ``_PUBLIC_ENDPOINTS`` (see ``app.py``) so the auth gate skips it.
"""
from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("landing", __name__)


@bp.route("/")
def index():
    return render_template("landing.html")
