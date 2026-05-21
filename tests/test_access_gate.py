"""The auth gate replaces the loopback guard: unauthenticated -> login."""
import pytest

from core import auth


@pytest.fixture()
def client(temp_db):
    auth.reset_lockouts()
    auth.set_password("pw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def test_health_is_public(client):
    resp = client.get("/health")
    assert resp.status_code in (200, 503)


def test_unauthenticated_redirects_to_login(client):
    resp = client.get("/")
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers["Location"]


def test_authenticated_reaches_index(client):
    client.post("/login", data={"password": "pw"})
    resp = client.get("/")
    assert resp.status_code == 200


def test_unauthenticated_xhr_gets_401(client):
    resp = client.get("/", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 401


def test_unknown_route_unauthenticated_is_404(client):
    resp = client.get("/this-route-does-not-exist-xyz")
    assert resp.status_code == 404


def test_allowed_hosts_rejects_foreign_host(temp_db, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "uploader.example.com")
    auth.set_password("pw")
    import importlib
    import app as flask_app_module
    importlib.reload(flask_app_module)  # rebuild create_app with the env set
    flask_app_module.app.config["TESTING"] = True
    try:
        with flask_app_module.app.test_client() as c:
            resp = c.get("/health", headers={"Host": "evil.example.com"})
            assert resp.status_code == 403
    finally:
        # Reload without ALLOWED_HOSTS so the module-level singleton is clean
        # for any subsequent test that imports `app` in this process.
        monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
        importlib.reload(flask_app_module)
