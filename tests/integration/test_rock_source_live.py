from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.integration


def _have_rock_session() -> bool:
    """Rock Playwright session lives in the encrypted secrets store.

    Legacy ``rock_session.json`` on disk is shredded post-migration. Also
    verifies decryption works — without SECRET_ENC_KEY set we can't read
    the blob, so we skip cleanly rather than fail with MasterKeyError.
    """
    try:
        from core import secrets_store
    except Exception:
        return False
    if not secrets_store.has_secret("playwright.rock_session"):
        return False
    try:
        return secrets_store.get_blob("playwright.rock_session") is not None
    except Exception:
        return False


@pytest.mark.skipif(not _have_rock_session(),
                    reason="Rock session secret missing")
def test_fetch_returns_items():
    from core.refresh import rock_source
    today = date.today()
    items = rock_source.fetch(today - timedelta(days=30), today + timedelta(days=180))
    assert isinstance(items, list)
    for it in items:
        assert it.platform == "rock"
        assert it.external_id.isdigit()
        assert it.iso_date
        assert it.status == "active"
