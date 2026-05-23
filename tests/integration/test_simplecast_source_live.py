from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.integration


def _have_simplecast_session() -> bool:
    """SimpleCast Playwright session lives in the encrypted secrets store.

    Legacy ``simplecast_session.json`` on disk is shredded post-migration.
    Also verifies decryption works — without SECRET_ENC_KEY set we can't
    read the blob, so we skip cleanly rather than fail with MasterKeyError.
    """
    try:
        from core import secrets_store
    except Exception:
        return False
    if not secrets_store.has_secret("playwright.simplecast_session"):
        return False
    try:
        return secrets_store.get_blob("playwright.simplecast_session") is not None
    except Exception:
        return False


@pytest.mark.skipif(not _have_simplecast_session(),
                    reason="SimpleCast session secret missing")
def test_fetch_returns_episodes():
    from core.refresh import simplecast_source
    today = date.today()
    items = simplecast_source.fetch(today - timedelta(days=180), today + timedelta(days=180))
    assert isinstance(items, list)
    for it in items:
        assert it.platform == "simplecast"
        assert it.external_id
        assert it.iso_date
        assert it.status in ("scheduled", "published")
