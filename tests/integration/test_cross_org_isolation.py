"""End-to-end: org A and org B never see each other's credentials.

Boots a real Flask app, creates two orgs, populates each with its own
youtube.token, and verifies the uploader's loader returns the right
token for the active session each time.
"""
from __future__ import annotations

import json
import pytest

from core import db, user_store, org_store, secrets_store


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


def test_two_orgs_two_tokens_no_leakage(app):
    org_a = org_store.create_org(name="A", slug="a")
    org_b = org_store.create_org(name="B", slug="b")
    secrets_store.set_secret("youtube.token", '{"t":"A"}', org_id=org_a["id"])
    secrets_store.set_secret("youtube.token", '{"t":"B"}', org_id=org_b["id"])
    secrets_store.set_platform_secret("youtube.client_secrets", "{}")

    user_a = user_store.create_user(
        username="ua", email="ua@x", password="pw1234567", program_owner=False,
    )
    user_b = user_store.create_user(
        username="ub", email="ub@x", password="pw1234567", program_owner=False,
    )
    org_store.add_membership(user_id=user_a["id"], org_id=org_a["id"], role="owner")
    org_store.add_membership(user_id=user_b["id"], org_id=org_b["id"], role="owner")

    from flask import session as flask_session
    from uploaders import youtube_uploader as yt
    with app.test_request_context():
        flask_session["user_id"] = user_a["id"]
        flask_session["current_org_id"] = org_a["id"]
        assert json.loads(yt._load_token_json())["t"] == "A"
    with app.test_request_context():
        flask_session["user_id"] = user_b["id"]
        flask_session["current_org_id"] = org_b["id"]
        assert json.loads(yt._load_token_json())["t"] == "B"


def test_owner_impersonating_reads_target_org_token(app):
    org_t = org_store.create_org(name="Target", slug="t")
    secrets_store.set_secret("youtube.token", '{"t":"target"}', org_id=org_t["id"])
    secrets_store.set_platform_secret("youtube.client_secrets", "{}")
    po_org = org_store.create_org(name="Boot", slug="boot")
    po = user_store.create_user(
        username="po", email="po@x", password="pw1234567", program_owner=True,
    )
    org_store.add_membership(user_id=po["id"], org_id=po_org["id"], role="owner")
    from flask import session as flask_session
    from uploaders import youtube_uploader as yt
    with app.test_request_context():
        flask_session["user_id"] = po["id"]
        flask_session["current_org_id"] = po_org["id"]
        flask_session["acting_as_org_id"] = org_t["id"]
        assert json.loads(yt._load_token_json())["t"] == "target"
