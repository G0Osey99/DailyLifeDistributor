"""Login/logout flow tests using the Flask test client."""
import pytest

from core import auth


@pytest.fixture()
def client(temp_db, monkeypatch):
    auth.reset_lockouts()
    auth.set_password("correct-horse")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def test_login_page_accessible_without_session(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"password" in resp.data.lower()


def test_login_success_sets_session(client):
    resp = client.post("/login", data={"password": "correct-horse"})
    assert resp.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("authenticated") is True


def test_login_failure(client):
    resp = client.post("/login", data={"password": "nope"})
    assert resp.status_code == 401
    with client.session_transaction() as sess:
        assert sess.get("authenticated") is None


def test_logout_clears_session(client):
    client.post("/login", data={"password": "correct-horse"})
    client.post("/logout")
    with client.session_transaction() as sess:
        assert sess.get("authenticated") is None


import pytest as _pytest


@_pytest.mark.parametrize("bad", ["//evil.com", "/\\evil.com", "https://evil.com", "evil.com"])
def test_login_rejects_open_redirect(client, bad):
    resp = client.post("/login", data={"password": "correct-horse"},
                       query_string={"next": bad})
    assert resp.status_code in (301, 302)
    assert "evil.com" not in resp.headers["Location"]


def test_login_allows_safe_relative_next(client):
    resp = client.post("/login", data={"password": "correct-horse"},
                       query_string={"next": "/settings"})
    assert resp.status_code in (301, 302)
    assert resp.headers["Location"].endswith("/settings")


def test_login_next_strips_whitespace(client):
    resp = client.post("/login", data={"password": "correct-horse"},
                       query_string={"next": " //evil.com"})
    assert resp.status_code in (301, 302)
    assert "evil.com" not in resp.headers["Location"]
