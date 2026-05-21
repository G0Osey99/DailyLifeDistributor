"""Hosted mode flips no_login_recovery on the upload session configs."""
import importlib

import pytest


def test_hosted_sets_no_login_recovery(monkeypatch):
    monkeypatch.setenv("HOSTED", "true")
    import uploaders.simplecast_uploader as sc
    importlib.reload(sc)
    assert sc._SC_SESSION_CONFIG_BASE.no_login_recovery is True


def test_not_hosted_allows_interactive(monkeypatch):
    monkeypatch.delenv("HOSTED", raising=False)
    import uploaders.simplecast_uploader as sc
    importlib.reload(sc)
    assert sc._SC_SESSION_CONFIG_BASE.no_login_recovery is False


@pytest.fixture(autouse=True)
def _restore():
    yield
    import uploaders.simplecast_uploader as sc
    importlib.reload(sc)
