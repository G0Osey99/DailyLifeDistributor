"""Unit tests for the remote-login state machine (fake browser, no Chrome)."""
import pytest
from flask import Flask

from core import remote_login
from core.playwright_session import SessionConfig


@pytest.fixture()
def app_ctx():
    app = Flask(__name__); app.secret_key = "t"
    with app.test_request_context():
        yield


class FakeBrowser:
    def __init__(self):
        self.goto_url = None
        self.closed = False
        self.saved_to = None

    def goto(self, url):
        self.goto_url = url

    def storage_state(self, path):
        self.saved_to = path
        with open(path, "w") as f:
            f.write('{"cookies": []}')

    def close(self):
        self.closed = True


def _cfg(tmp_path):
    return SessionConfig(
        name="simplecast",
        session_file=str(tmp_path / "simplecast_session.json"),
        is_login_url=lambda u: "login" in u,
        login_url="https://app.simplecast.com/login",
    )


@pytest.fixture
def mgr():
    created = []

    def launcher(config):
        b = FakeBrowser()
        created.append(b)
        return b

    m = remote_login.RemoteLoginManager(browser_launcher=launcher, idle_timeout_s=600)
    m._created = created  # test handle
    return m


def test_starts_idle(mgr):
    st = mgr.status()
    assert st.active is False
    assert st.phase == "idle"


def test_start_launches_and_navigates(mgr, tmp_path):
    mgr.start("simplecast", _cfg(tmp_path))
    st = mgr.status()
    assert st.active is True
    assert st.service == "simplecast"
    assert st.phase == "awaiting_login"
    assert mgr._created[0].goto_url == "https://app.simplecast.com/login"


def test_single_instance_lock(mgr, tmp_path):
    mgr.start("simplecast", _cfg(tmp_path))
    with pytest.raises(remote_login.RemoteLoginError):
        mgr.start("rock", _cfg(tmp_path))


def test_cancel_tears_down(mgr, tmp_path):
    mgr.start("simplecast", _cfg(tmp_path))
    browser = mgr._created[0]
    mgr.cancel()
    assert browser.closed is True
    assert mgr.status().active is False
    assert mgr.status().phase == "idle"


def test_idle_timeout_tears_down(tmp_path):
    fake_clock = {"t": 1000.0}

    def launcher(config):
        return FakeBrowser()

    m = remote_login.RemoteLoginManager(
        browser_launcher=launcher, idle_timeout_s=300,
        clock=lambda: fake_clock["t"],
    )
    m.start("simplecast", _cfg(tmp_path))
    fake_clock["t"] = 1000.0 + 301
    m.poll_timeout()
    assert m.status().active is False
    assert m.status().phase == "idle"


def test_save_success_flow(app_ctx, mgr, tmp_path, temp_db):
    cfg = _cfg(tmp_path)
    mgr.start("simplecast", cfg)
    browser = mgr._created[0]
    mgr.save()
    st = mgr.status()
    assert st.phase == "done"
    assert st.active is False
    assert browser.closed is True
    assert browser.saved_to == cfg.session_file


def test_save_failure_sets_error_and_tears_down(tmp_path, temp_db):
    class BoomBrowser(FakeBrowser):
        def storage_state(self, path):
            raise RuntimeError("boom")

    created = []

    def launcher(config):
        b = BoomBrowser()
        created.append(b)
        return b

    m = remote_login.RemoteLoginManager(browser_launcher=launcher)
    m.start("simplecast", _cfg(tmp_path))
    with pytest.raises(RuntimeError):
        m.save()
    st = m.status()
    assert st.phase == "error"
    assert "boom" in st.message
    assert created[0].closed is True
    assert st.active is False


def test_save_without_session_raises(mgr):
    with pytest.raises(remote_login.RemoteLoginError):
        mgr.save()


def test_on_teardown_called_on_cancel(tmp_path):
    calls = []
    m = remote_login.RemoteLoginManager(
        browser_launcher=lambda c: FakeBrowser(),
        on_teardown=lambda: calls.append("t"),
    )
    m.start("simplecast", _cfg(tmp_path))
    m.cancel()
    assert calls == ["t"]


def test_on_teardown_called_on_save(app_ctx, tmp_path, temp_db):
    calls = []
    m = remote_login.RemoteLoginManager(
        browser_launcher=lambda c: FakeBrowser(),
        on_teardown=lambda: calls.append("t"),
    )
    m.start("simplecast", _cfg(tmp_path))
    m.save()
    assert calls == ["t"]


def test_on_teardown_called_on_idle(tmp_path):
    clk = {"t": 1000.0}
    calls = []
    m = remote_login.RemoteLoginManager(
        browser_launcher=lambda c: FakeBrowser(),
        idle_timeout_s=300, clock=lambda: clk["t"],
        on_teardown=lambda: calls.append("t"),
    )
    m.start("simplecast", _cfg(tmp_path))
    clk["t"] += 301
    m.poll_timeout()
    assert calls == ["t"]


def test_on_teardown_not_called_without_active_session():
    calls = []
    m = remote_login.RemoteLoginManager(
        browser_launcher=lambda c: FakeBrowser(),
        on_teardown=lambda: calls.append("t"),
    )
    m.cancel()  # nothing was active
    assert calls == []


def test_save_encrypts_session_into_store(app_ctx, tmp_path, temp_db):
    from flask import session as flask_session
    flask_session["current_org_id"] = 1

    created = []

    def launcher(config):
        b = FakeBrowser()
        created.append(b)
        return b

    m = remote_login.RemoteLoginManager(browser_launcher=launcher)
    cfg = _cfg(tmp_path)
    m.start("simplecast", cfg)
    m.save()

    from core import secrets_store
    from core.playwright_session import _session_secret_name
    secret_name = _session_secret_name(cfg.session_file)

    # After Fix 1 the blob must land in the org-scoped slot.
    blob = secrets_store.get_blob(secret_name, org_id=1)
    assert blob == b'{"cookies": []}'

    # The legacy unscoped slot must remain empty — production must not write there.
    assert secrets_store.get_blob(secret_name) is None

    assert created[0].closed is True            # browser torn down after save
    assert m.status().phase == "done"
