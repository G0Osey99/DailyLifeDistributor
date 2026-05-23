import pytest


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import importlib
    import app as flask_app_module
    importlib.reload(flask_app_module)
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def test_get_login_renders_username_field(client, monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "false")
    resp = client.get("/login")
    body = resp.data.decode("utf-8")
    assert 'name="username"' in body
    assert 'name="password"' in body


def test_get_login_legacy_mode_only_password(client, monkeypatch):
    monkeypatch.setenv("LEGACY_PASSWORD_ENABLED", "true")
    resp = client.get("/login")
    body = resp.data.decode("utf-8")
    assert 'name="password"' in body
    # Legacy form is single-field — no username input shown.
    assert 'name="username"' not in body
