"""Remote-login control routes: auth-gated + state transitions (fake browser)."""
import pytest

from core import auth, remote_login
from core.playwright_session import SessionConfig


class FakeBrowser:
    def __init__(self):
        self.closed = False
    def goto(self, url):
        pass
    def storage_state(self, path):
        with open(path, "w") as f:
            f.write('{"cookies": []}')
    def close(self):
        self.closed = True


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    auth.reset_lockouts()
    auth.set_password("pw")
    import blueprints.remote_login as rl

    # Swap the module manager for one with a fake launcher + a known service cfg.
    cfg = SessionConfig(name="simplecast",
                        session_file=str(tmp_path / "simplecast_session.json"),
                        is_login_url=lambda u: "login" in u,
                        login_url="https://app.simplecast.com/login")
    monkeypatch.setattr(rl, "manager",
                        remote_login.RemoteLoginManager(browser_launcher=lambda c: FakeBrowser()))
    monkeypatch.setattr(rl, "_service_configs", lambda: {"simplecast": cfg})

    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        yield c


def test_status_requires_auth(client):
    # Fresh client w/o login — but fixture already logged in; use a new client.
    import app as flask_app_module
    with flask_app_module.app.test_client() as anon:
        resp = anon.get("/remote-login/status")
        assert resp.status_code in (301, 302)  # redirected to /login


def test_start_then_save_flow(client):
    client.post("/login", data={"password": "pw"})
    r = client.post("/remote-login/start", data={"service": "simplecast"})
    assert r.status_code == 200 and r.get_json()["status"]["phase"] == "awaiting_login"
    r2 = client.post("/remote-login/save")
    assert r2.status_code == 200 and r2.get_json()["status"]["phase"] == "done"


def test_unknown_service(client):
    client.post("/login", data={"password": "pw"})
    r = client.post("/remote-login/start", data={"service": "nope"})
    assert r.status_code == 400
