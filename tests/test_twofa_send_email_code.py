"""POST /settings/2fa/send-email-code — issuing a fresh 2FA email code.

Behaviour pinned here:
  * Unauthenticated requests bounce to /login (the route is gated by
    @login_required → 302).
  * If the user does NOT have email 2FA enabled, the route is a no-op:
    no code is minted, the user is redirected with a flash.
  * If the user DOES have email 2FA enabled, ``email_2fa.generate_login_code``
    is invoked exactly once.
"""
from __future__ import annotations

import pytest

from core import db as _db
from core import user_store


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("HYBRID_AGENT_ENABLED", "false")
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-twofa-email")
    monkeypatch.setenv("HOSTED", "")
    from app import create_app
    a = create_app()
    a.config["TESTING"] = True
    yield a


@pytest.fixture
def client(app):
    return app.test_client()


def _make_user(*, email_2fa: bool):
    u = user_store.create_user(
        username="alice", email="alice@example.com",
        password="long-enough-pw-12!",
    )
    user_store.update_password(u["id"], "long-enough-pw-12!")
    if email_2fa:
        _db.set_user_email_2fa(u["id"], True)
    return u


def test_send_email_code_requires_auth(client):
    r = client.post("/settings/2fa/send-email-code", follow_redirects=False)
    # @login_required → 302 to login (or 401 for json clients). Here a
    # form POST gets the redirect.
    assert r.status_code in (302, 401)


def test_send_email_code_noop_when_email_2fa_disabled(client, monkeypatch):
    u = _make_user(email_2fa=False)
    with client.session_transaction() as s:
        s["user_id"] = u["id"]

    called = {"n": 0}
    from core import email_2fa as _e2fa
    monkeypatch.setattr(_e2fa, "generate_login_code",
                        lambda uid: called.__setitem__("n", called["n"] + 1))
    # The blueprint imported the symbol as `_email_2fa`; patch on the
    # blueprint namespace too so the route uses our shim.
    from blueprints import twofa as _twofa
    monkeypatch.setattr(_twofa._email_2fa, "generate_login_code",
                        lambda uid: called.__setitem__("n", called["n"] + 1))

    r = client.post("/settings/2fa/send-email-code", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert called["n"] == 0, "must NOT mint a code when email 2FA is disabled"


def test_send_email_code_invokes_generator_once_when_enabled(client, monkeypatch):
    u = _make_user(email_2fa=True)
    with client.session_transaction() as s:
        s["user_id"] = u["id"]

    calls: list[int] = []
    from blueprints import twofa as _twofa
    monkeypatch.setattr(
        _twofa._email_2fa, "generate_login_code",
        lambda uid: (calls.append(uid), "123456")[1],
    )

    r = client.post("/settings/2fa/send-email-code", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert calls == [u["id"]]
