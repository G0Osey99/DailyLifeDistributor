"""Hosted (web redirect) YouTube OAuth flow.

The desktop loopback flow can't authenticate a remote user on the headless
VPS; these cover the web-redirect helpers and the callback's CSRF guard.
"""
from urllib.parse import quote

import pytest

from core import auth
from uploaders import youtube_uploader as yt

REDIRECT = "https://autoalert.pro/oauth/youtube/callback"

_WEB_CFG = {
    "web": {
        "client_id": "test-client.apps.googleusercontent.com",
        "project_id": "proj",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "shh",
        "redirect_uris": [REDIRECT],
    }
}

_INSTALLED_CFG = {
    "installed": {
        "client_id": "x.apps.googleusercontent.com",
        "client_secret": "shh",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


def test_build_web_flow_rejects_desktop_client(monkeypatch):
    monkeypatch.setattr(yt, "_load_client_config", lambda: _INSTALLED_CFG)
    with pytest.raises(RuntimeError) as exc:
        yt.build_web_flow(REDIRECT)
    assert "Web application" in str(exc.value)


def test_start_web_authorization_builds_consent_url(monkeypatch):
    monkeypatch.setattr(yt, "_load_client_config", lambda: _WEB_CFG)
    url, state = yt.start_web_authorization(REDIRECT)
    assert state
    assert "accounts.google.com" in url
    assert "test-client.apps.googleusercontent.com" in url
    assert quote(REDIRECT, safe="") in url        # redirect_uri round-trips
    assert "access_type=offline" in url           # asks for a refresh token


def test_finish_web_authorization_saves_token(monkeypatch):
    saved = {}
    monkeypatch.setattr(yt, "_save_token_json",
                        lambda data: saved.__setitem__("token", data))

    class _Creds:
        def to_json(self):
            return '{"refresh_token": "rt"}'

    class _Flow:
        def __init__(self):
            self.credentials = _Creds()
            self.fetched = None

        def fetch_token(self, authorization_response=None):
            self.fetched = authorization_response

    fake = _Flow()
    monkeypatch.setattr(yt, "build_web_flow",
                        lambda redirect_uri, state=None: fake)

    auth_response = REDIRECT + "?code=abc&state=st8"
    yt.finish_web_authorization(REDIRECT, "st8", auth_response)

    assert fake.fetched == auth_response
    assert saved["token"] == '{"refresh_token": "rt"}'


@pytest.fixture()
def logged_in_client(temp_db):
    auth.reset_lockouts()
    auth.set_password("pw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        c.post("/login", data={"password": "pw"})
        yield c


def test_callback_rejects_state_mismatch(logged_in_client):
    # No yt_oauth_state was stashed → the CSRF check must fail and bounce the
    # user back to settings rather than exchange the code.
    resp = logged_in_client.get("/oauth/youtube/callback?state=forged&code=x")
    assert resp.status_code in (301, 302)
    assert "/settings" in resp.headers["Location"]
