"""Clearing the YouTube token via Settings clears the stored secret."""
import json

import pytest

from core import auth
from uploaders import youtube_uploader as yt


@pytest.fixture()
def client(temp_db):
    auth.reset_lockouts()
    auth.set_password("pw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        c.post("/login", data={"password": "pw"})
        yield c


def test_clear_route_removes_stored_token(client):
    yt._save_token_json(json.dumps({"refresh_token": "abc"}))
    assert yt._load_token_json() is not None
    resp = client.post("/settings/clear-youtube-token")
    assert resp.status_code in (301, 302)
    assert yt._load_token_json() is None
