"""YouTube token round-trips through the encrypted store."""
import json

import pytest
from flask import Flask

from core import secrets_store
from uploaders import youtube_uploader as yt

_ORG_ID = 42


@pytest.fixture(autouse=True)
def _db(temp_db):
    yield


@pytest.fixture()
def app_ctx():
    """Minimal Flask request context with a current_org_id set."""
    app = Flask(__name__)
    app.secret_key = "t"
    with app.test_request_context():
        from flask import session
        session["current_org_id"] = _ORG_ID
        yield


def test_token_save_load_clear(app_ctx):
    assert yt._load_token_json() is None
    yt._save_token_json(json.dumps({"refresh_token": "abc"}))
    assert json.loads(yt._load_token_json())["refresh_token"] == "abc"
    yt._clear_token()
    assert yt._load_token_json() is None


def test_token_encrypted_at_rest(app_ctx):
    yt._save_token_json(json.dumps({"refresh_token": "SENSITIVE"}))
    # Token is now stored under the org-scoped name.
    from core.db import _get_conn
    from core import secrets_store as _ss
    storage_name = f"org:{_ORG_ID}:{yt._YT_TOKEN_NAME}"
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM secrets WHERE name=?", (storage_name,)
        ).fetchone()
    assert row is not None, "org-scoped token row not found"
    assert b"SENSITIVE" not in bytes(row["value"])


def test_client_secrets_materializes_to_file():
    secrets_store.set_blob(yt._YT_CLIENT_SECRETS_NAME, b'{"installed": {}}')
    with secrets_store.materialize_blob_to_tempfile(
        yt._YT_CLIENT_SECRETS_NAME, suffix=".json"
    ) as path:
        with open(path) as f:
            assert json.load(f) == {"installed": {}}


def test_corrupt_stored_token_raises_actionable_error(app_ctx):
    """Building credentials from corrupt stored JSON must fail loudly."""
    yt._save_token_json("this is not valid json")
    with pytest.raises(RuntimeError) as exc_info:
        # Corrupt stored token JSON must raise with an actionable message,
        # the same way a corrupt token.json used to.
        yt.get_authenticated_service()
    assert "corrupt" in str(exc_info.value).lower()
    assert "Clear YouTube Token" in str(exc_info.value)
