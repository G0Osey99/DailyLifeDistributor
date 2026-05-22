"""The /health probe's structure and failure semantics.

The existing access-gate test only asserts /health is public. This pins the
contract on-call relies on: the three subsystem checks are reported, and a
broken dependency flips the overall verdict to 503.
"""
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


def test_health_ok_reports_all_checks(client, monkeypatch):
    # Patch the LLM probe so the test neither waits on a 5s network timeout nor
    # depends on a running endpoint; the DB + chrome checks run for real.
    import core.llm_title_gen as llm
    monkeypatch.setattr(llm, "is_llamafile_running", lambda: True)

    resp = client.get("/health")
    data = resp.get_json()

    assert set(data["checks"]) >= {"db", "llamafile", "chrome"}
    assert data["checks"]["db"]["ok"] is True
    assert data["ok"] is True
    assert resp.status_code == 200


def test_health_503_when_db_unavailable(client, monkeypatch):
    import core.llm_title_gen as llm
    monkeypatch.setattr(llm, "is_llamafile_running", lambda: True)

    from core import db as _db

    def _boom():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(_db, "_get_conn", _boom)

    resp = client.get("/health")
    data = resp.get_json()

    assert resp.status_code == 503
    assert data["ok"] is False
    assert data["checks"]["db"]["ok"] is False


def test_health_503_when_llm_down(client, monkeypatch):
    import core.llm_title_gen as llm
    monkeypatch.setattr(llm, "is_llamafile_running", lambda: False)

    resp = client.get("/health")
    data = resp.get_json()

    assert resp.status_code == 503
    assert data["checks"]["llamafile"]["ok"] is False
    # The DB is still fine — the failure is isolated to the LLM check.
    assert data["checks"]["db"]["ok"] is True
