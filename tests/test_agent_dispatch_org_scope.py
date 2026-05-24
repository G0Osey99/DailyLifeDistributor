"""agent_dispatch ships the effective org's credentials, not the legacy slot."""
from __future__ import annotations

import pytest
from flask import Flask

from core import db, secrets_store
from core import agent_dispatch


@pytest.fixture()
def app_ctx():
    app = Flask(__name__); app.secret_key = "t"
    with app.test_request_context():
        yield


def test_collect_credentials_pulls_from_effective_org(app_ctx):
    from flask import session
    secrets_store.set_secret("youtube.token", "tok-A", org_id=1)
    secrets_store.set_secret("youtube.token", "tok-B", org_id=2)
    secrets_store.set_platform_secret("youtube.client_secrets", "{}")
    session["current_org_id"] = 1
    creds = agent_dispatch.collect_credentials(
        platforms_in_use={"YouTube Video"},
    )
    assert creds["youtube.token"] == "tok-A"
    assert creds["youtube.client_secrets"] == "{}"


def test_collect_credentials_under_impersonation(app_ctx):
    from flask import session
    secrets_store.set_secret("youtube.token", "tok-A", org_id=1)
    secrets_store.set_secret("youtube.token", "tok-B", org_id=2)
    secrets_store.set_platform_secret("youtube.client_secrets", "{}")
    session["current_org_id"] = 1
    session["acting_as_org_id"] = 2
    creds = agent_dispatch.collect_credentials(
        platforms_in_use={"YouTube Video"},
    )
    assert creds["youtube.token"] == "tok-B"


def test_envelope_carries_org_id(app_ctx):
    from flask import session
    session["current_org_id"] = 7
    env = agent_dispatch.build_envelope(
        job_id="job1", rows=[], entries={}, credentials={}, config={},
    )
    assert env["payload"]["org_id"] == 7
