"""Verify that uploading client_secrets.json via Settings also writes the
encrypted store under 'youtube.client_secrets'."""
import io
import json

import pytest

from core import auth, secrets_store


_FAKE_SECRETS = json.dumps(
    {"installed": {"client_id": "test-id", "client_secret": "test-secret"}}
).encode()


@pytest.fixture()
def logged_in_client(temp_db):
    auth.reset_lockouts()
    auth.set_password("pw")
    import app as flask_app_module

    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        c.post("/login", data={"password": "pw"})
        yield c


def test_client_secrets_upload_persists_to_store(logged_in_client, tmp_path, monkeypatch):
    """POST an uploaded client_secrets.json and assert the encrypted store is populated."""
    # Redirect the disk write so we don't litter the real project root.
    monkeypatch.setattr(
        "blueprints.settings.PROJECT_ROOT", str(tmp_path), raising=True
    )

    data = {
        "client_secrets_file": (io.BytesIO(_FAKE_SECRETS), "client_secrets.json"),
        # Minimal required form fields so the config-yaml save succeeds.
        "sched_youtube_video": "10:00",
        "sched_youtube_shorts": "12:00",
        "sched_simplecast": "06:00",
        "sched_vista_social": "12:00",
        "sched_timezone": "America/New_York",
        "yt_default_privacy": "private",
        "yt_category_id": "22",
        "llm_model": "llama3.2",
        "llm_num_titles": "5",
        "whisper_model": "base",
        "dir_base": "",
        "dir_youtube_video": "",
        "dir_youtube_shorts": "",
        "dir_podcast": "",
        "dir_thumbnails": "",
    }
    resp = logged_in_client.post(
        "/settings",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    # Settings POST always redirects on success (301/302).
    assert resp.status_code in (301, 302), f"Unexpected status {resp.status_code}"

    stored = secrets_store.get_blob("youtube.client_secrets")
    assert stored is not None, "secrets_store.get_blob('youtube.client_secrets') returned None"
    assert stored == _FAKE_SECRETS
