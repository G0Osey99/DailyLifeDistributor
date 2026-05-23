from datetime import date, timedelta

import pytest

from tests.integration._creds_helpers import safely_has_credential

pytestmark = pytest.mark.integration


def _have_rock_session() -> bool:
    """Rock Playwright session lives in the encrypted secrets store.

    Legacy ``rock_session.json`` on disk is shredded post-migration. The
    helper returns False on CI / fresh installs where the DB is unmigrated
    (no ``secrets`` table), where ``SECRET_ENC_KEY`` is missing, or where
    decryption otherwise fails — so the test skips cleanly instead of
    erroring at collection time.
    """
    return safely_has_credential("playwright.rock_session")


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
