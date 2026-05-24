"""client_secrets upload is admin-only; per-org users don't see the row."""
from __future__ import annotations

import io
import json
import pytest
from flask import Flask

from core import db, user_store, org_store
from core import secrets_store


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SECRET_ENC_KEY", "QcOzn6q5y8yp7v4OoH5sNcWZS_VqIyqU0o8jOwHjW6w=")
    monkeypatch.setenv("FLASK_SECRET_KEY", "t")
    import importlib
    import core.db, core.org_store, core.user_store, core.secrets_store
    importlib.reload(core.db); importlib.reload(core.org_store)
    importlib.reload(core.user_store); importlib.reload(core.secrets_store)
    core.db.init_db()
    import app as m; importlib.reload(m)
    return m.app


def _login_as(client, user_id, org_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["current_org_id"] = org_id
        sess["permission_2fa_passed"] = True


def test_org_owner_does_not_see_client_secrets_row(app):
    org = org_store.create_org(name="A", slug="a")
    owner = user_store.create_user(
        username="alice", email="a@x", password="pw1234567", program_owner=False,
    )
    org_store.add_membership(user_id=owner["id"], org_id=org["id"], role="owner")
    client = app.test_client()
    _login_as(client, owner["id"], org["id"])
    res = client.get("/settings")
    assert res.status_code == 200
    assert b"client_secrets.json" not in res.data


def test_org_owner_post_to_client_secrets_returns_403(app):
    org = org_store.create_org(name="A", slug="a")
    owner = user_store.create_user(
        username="alice", email="a@x", password="pw1234567", program_owner=False,
    )
    org_store.add_membership(user_id=owner["id"], org_id=org["id"], role="owner")
    client = app.test_client()
    _login_as(client, owner["id"], org["id"])
    res = client.post("/settings", data={
        "youtube_client_secrets": (io.BytesIO(json.dumps(
            {"web": {"client_id": "x"}}).encode()), "client_secrets.json"),
    }, content_type="multipart/form-data")
    assert res.status_code in (403, 302)
    assert secrets_store.has_platform_secret("youtube.client_secrets") is False


def test_program_owner_upload_lands_in_platform_scope(app):
    org = org_store.create_org(name="LCBC", slug="lcbc")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=org["id"], role="owner")
    client = app.test_client()
    _login_as(client, po["id"], org["id"])
    res = client.post("/settings", data={
        "youtube_client_secrets": (io.BytesIO(json.dumps(
            {"web": {"client_id": "x"}}).encode()), "client_secrets.json"),
    }, content_type="multipart/form-data", follow_redirects=False)
    assert res.status_code in (200, 302)
    assert secrets_store.has_platform_secret("youtube.client_secrets") is True
    assert secrets_store.has_secret("youtube.client_secrets") is False
    assert secrets_store.has_secret("youtube.client_secrets", org_id=org["id"]) is False
