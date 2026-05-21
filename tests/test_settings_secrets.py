"""Settings secret management + password change."""
import pytest

from core import auth, secrets_store


@pytest.fixture()
def client(temp_db):
    auth.reset_lockouts()
    auth.set_password("oldpw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        c.post("/login", data={"password": "oldpw"})
        yield c


def test_set_and_clear_secret(client):
    client.post("/settings/set-secret", data={"name": "PEXELS_API_KEY", "value": "p"})
    assert secrets_store.get_secret("PEXELS_API_KEY") == "p"
    client.post("/settings/clear-secret", data={"name": "PEXELS_API_KEY"})
    assert secrets_store.get_secret("PEXELS_API_KEY") is None


def test_change_password_requires_current(client):
    client.post("/settings/change-password", data={"current": "wrong", "new": "newpw"})
    assert auth.verify_password("oldpw") is True  # unchanged
    client.post("/settings/change-password", data={"current": "oldpw", "new": "newpw"})
    assert auth.verify_password("newpw") is True
