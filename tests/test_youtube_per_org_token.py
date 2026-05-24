"""YouTube uploader reads/writes the token per-org and the client_secrets per-platform."""
from __future__ import annotations

import json
import pytest
from flask import Flask

from core import db, secrets_store
from uploaders import youtube_uploader as yt


@pytest.fixture(autouse=True)
def _iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DLD_STATE_DB", str(tmp_path / "state.db"))
    import importlib
    importlib.reload(db); db.init_db()
    yield


@pytest.fixture()
def app_ctx():
    app = Flask(__name__); app.secret_key = "t"
    with app.test_request_context():
        yield


def test_load_token_reads_from_effective_org(app_ctx):
    from flask import session
    secrets_store.set_secret("youtube.token", '{"t":"a"}', org_id=1)
    secrets_store.set_secret("youtube.token", '{"t":"b"}', org_id=2)
    session["current_org_id"] = 1
    assert json.loads(yt._load_token_json())["t"] == "a"
    session["current_org_id"] = 2
    assert json.loads(yt._load_token_json())["t"] == "b"


def test_load_token_under_impersonation_reads_target(app_ctx):
    from flask import session
    secrets_store.set_secret("youtube.token", '{"t":"a"}', org_id=1)
    secrets_store.set_secret("youtube.token", '{"t":"b"}', org_id=2)
    session["current_org_id"] = 1
    session["acting_as_org_id"] = 2
    assert json.loads(yt._load_token_json())["t"] == "b"


def test_save_token_lands_in_effective_org(app_ctx):
    from flask import session
    session["current_org_id"] = 7
    yt._save_token_json('{"t":"new"}')
    assert secrets_store.get_secret("youtube.token", org_id=7) == '{"t":"new"}'
    # Legacy slot must not be written.
    assert secrets_store.get_secret("youtube.token") is None


def test_client_secrets_reads_platform_scope(app_ctx):
    secrets_store.set_platform_secret(
        "youtube.client_secrets",
        '{"web":{"client_id":"X"}}',
    )
    cfg = yt._load_client_config()
    assert cfg["web"]["client_id"] == "X"


def test_clear_token_removes_only_current_org(app_ctx):
    from flask import session
    secrets_store.set_secret("youtube.token", "a", org_id=1)
    secrets_store.set_secret("youtube.token", "b", org_id=2)
    session["current_org_id"] = 1
    yt._clear_token()
    assert secrets_store.get_secret("youtube.token", org_id=1) is None
    assert secrets_store.get_secret("youtube.token", org_id=2) == "b"
