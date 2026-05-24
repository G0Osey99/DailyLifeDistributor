"""Verify that uploading client_secrets.json via Settings also writes the
encrypted store under the platform-scoped 'youtube.client_secrets' key."""
import io
import json

import pytest

from core import db, user_store, org_store
from core import secrets_store


_FAKE_SECRETS = json.dumps(
    {"installed": {"client_id": "test-id", "client_secret": "test-secret"}}
).encode()


def _login_as_program_owner(client, user_id, org_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["current_org_id"] = org_id
        sess["permission_2fa_passed"] = True


@pytest.fixture()
def program_owner_client(app):
    """A test client authenticated as a program_owner user."""
    org = org_store.create_org(name="TestOrg", slug="testorg")
    po = user_store.create_user(
        username="po_upload", email="po_upload@example.com",
        password="long-enough-pw-12!", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=org["id"], role="owner")
    c = app.test_client()
    _login_as_program_owner(c, po["id"], org["id"])
    return c


def test_client_secrets_upload_persists_to_store(program_owner_client, tmp_path, monkeypatch):
    """POST an uploaded client_secrets.json and assert the encrypted store is populated."""
    # Redirect the disk write so we don't litter the real project root.
    monkeypatch.setattr(
        "blueprints.settings.PROJECT_ROOT", str(tmp_path), raising=True
    )

    data = {
        "youtube_client_secrets": (io.BytesIO(_FAKE_SECRETS), "client_secrets.json"),
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
        "dir_base": "",
        "dir_youtube_video": "",
        "dir_youtube_shorts": "",
        "dir_podcast": "",
        "dir_thumbnails": "",
    }
    resp = program_owner_client.post(
        "/settings",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    # Settings POST always redirects on success (301/302).
    assert resp.status_code in (301, 302), f"Unexpected status {resp.status_code}"

    stored = secrets_store.get_platform_blob("youtube.client_secrets")
    assert stored is not None, "secrets_store.get_platform_blob('youtube.client_secrets') returned None"
    assert stored == _FAKE_SECRETS
