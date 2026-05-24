"""Public marketing landing page at /.

The dashboard moved from / to /dashboard; / is now an unauthenticated
entry point. These tests pin:
  - the route is reachable without auth
  - it renders the expected hero markup
  - sign-in CTAs point at /dashboard
  - the auth gate's _PUBLIC_ENDPOINTS exemption is wired correctly
"""
from __future__ import annotations

import pytest

from core import auth


@pytest.fixture()
def client(temp_db, monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
    auth.reset_lockouts()
    auth.set_password("pw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def test_landing_page_is_public(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_landing_page_contains_hero_copy(client):
    resp = client.get("/")
    assert b"Daily Life Distributor" in resp.data
    assert b"One day's media" in resp.data
    assert b"Every platform" in resp.data


def test_landing_page_ctas_link_to_dashboard(client):
    resp = client.get("/")
    # Two primary CTAs and a footer link all point at /dashboard.
    body = resp.data.decode("utf-8")
    assert body.count('href="/dashboard"') >= 3


def test_landing_page_strips_react_and_tweaks_panel(client):
    """The design's React / Babel / Tweaks tooling is for the design canvas
    only. The shipped page must not pull React or the in-browser Babel
    compiler — the live preview is plain vanilla JS."""
    body = client.get("/").data
    assert b"react.development.js" not in body
    assert b"babel" not in body.lower()
    assert b"__tweaks-root" not in body


def test_landing_page_does_not_trigger_redirect_when_authed(client):
    """An authenticated user visiting / still gets the landing page (200),
    not a redirect to /dashboard. We don't force authed users off the
    marketing page — they can navigate to the dashboard via the CTA."""
    client.post("/login", data={"password": "pw"})
    resp = client.get("/")
    assert resp.status_code == 200
