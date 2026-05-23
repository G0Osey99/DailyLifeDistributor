from datetime import date, timedelta

import pytest

from tests.integration._creds_helpers import safely_has_credential

pytestmark = pytest.mark.integration


def _have_simplecast_session() -> bool:
    """SimpleCast Playwright session lives in the encrypted secrets store.

    Legacy ``simplecast_session.json`` on disk is shredded post-migration.
    The helper returns False on CI / fresh installs where the DB is
    unmigrated (no ``secrets`` table), where ``SECRET_ENC_KEY`` is missing,
    or where decryption otherwise fails — so the test skips cleanly instead
    of erroring at collection time.
    """
    return safely_has_credential("playwright.simplecast_session")


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
