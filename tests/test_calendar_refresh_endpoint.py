import pytest

from core import auth


@pytest.fixture
def client(temp_db, monkeypatch):
    """Spin up the Flask test client with an isolated DB and stubbed sources."""
    import app as appmod

    auth.reset_lockouts()
    auth.set_password("test-pw")

    class StubYT:
        NAME = "youtube_video"
        PLATFORMS = ["youtube_video"]
        @staticmethod
        def fetch(s, e):
            from core.calendar_refresh import ExternalItem
            return [ExternalItem("youtube_video", "v1", str(s), "", "t", "u", "scheduled", "{}")]

    monkeypatch.setattr(
        "core.calendar_refresh.get_configured_sources",
        lambda: [StubYT],
    )
    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()
    client.post("/login", data={"password": "test-pw"})
    return client


def test_refresh_endpoint_returns_results(client):
    resp = client.post("/calendar/refresh")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "results" in data
    assert data["results"]["youtube_video"]["ok"] is True


def test_refresh_endpoint_returns_409_when_busy(client, monkeypatch):
    """Force the lock held to simulate concurrent run."""
    import core.calendar_refresh as cr
    cr._LOCK.acquire()
    try:
        resp = client.post("/calendar/refresh")
        assert resp.status_code == 409
    finally:
        cr._LOCK.release()
