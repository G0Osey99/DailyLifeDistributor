import pytest
from core import user_store, org_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-for-cookies")
    import importlib
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def _seed_user_with_active_password(username, email, pw):
    u = user_store.create_user(username=username, email=email, password="bootstrap1234!")
    user_store.update_password(u["id"], pw)
    return u


def test_login_with_valid_username_password_redirects(client):
    _seed_user_with_active_password("alice", "alice@x.com", "validpw123456!")
    resp = client.post(
        "/login",
        data={"username": "alice", "password": "validpw123456!"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with client.session_transaction() as s:
        assert s.get("user_id") is not None
        assert s.get("authenticated") is None  # legacy flag NOT set


def test_login_unknown_username_401(client):
    resp = client.post(
        "/login",
        data={"username": "nobody", "password": "x"},
    )
    assert resp.status_code == 401


def test_login_wrong_password_401(client):
    _seed_user_with_active_password("bob", "bob@x.com", "rightpw1234567!")
    resp = client.post(
        "/login",
        data={"username": "bob", "password": "wrongpw1234567!"},
    )
    assert resp.status_code == 401


def test_login_sets_current_org_id_to_first_membership(client):
    u = _seed_user_with_active_password("carol", "c@x.com", "pw1234567890!")
    o = org_store.create_org(name="O", slug="o", created_by_user_id=u["id"])
    org_store.add_membership(user_id=u["id"], org_id=o["id"], role="owner")
    client.post(
        "/login",
        data={"username": "carol", "password": "pw1234567890!"},
    )
    with client.session_transaction() as s:
        assert s.get("user_id") == u["id"]
        assert s.get("current_org_id") == o["id"]


def test_login_with_no_memberships_sets_current_org_id_none(client):
    _seed_user_with_active_password("dave", "d@x.com", "pw1234567890!")
    client.post(
        "/login",
        data={"username": "dave", "password": "pw1234567890!"},
    )
    with client.session_transaction() as s:
        assert s.get("user_id") is not None
        assert s.get("current_org_id") is None


def test_login_user_with_unchanged_password_redirects_to_first_set(client):
    # NEVER-CHANGED user (password_changed_at IS NULL): the seed password
    # is accepted, but the user is routed to /login/first-password-set
    # to pick a new one before getting a session.
    user_store.create_user(username="eve", email="e@x.com", password="originalpw!1234")
    resp = client.post(
        "/login",
        data={"username": "eve", "password": "originalpw!1234"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/first-password-set" in resp.headers.get("Location", "")
    with client.session_transaction() as s:
        assert s.get("user_id") is None


def test_login_user_with_wrong_password_still_401(client):
    user_store.create_user(username="eve", email="e@x.com", password="originalpw!1234")
    resp = client.post(
        "/login",
        data={"username": "eve", "password": "WRONG!1234567"},
    )
    assert resp.status_code == 401
