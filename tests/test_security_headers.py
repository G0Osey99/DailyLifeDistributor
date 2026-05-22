"""Defense-in-depth response headers are present on every response."""
import pytest

from core import auth


@pytest.fixture()
def client(temp_db, monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    auth.reset_lockouts()
    auth.set_password("pw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def test_security_headers_on_login(client):
    resp = client.get("/login")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert resp.headers.get("Referrer-Policy")


def test_hsts_present_when_secure(client):
    resp = client.get("/login")
    assert "max-age=" in resp.headers.get("Strict-Transport-Security", "")
