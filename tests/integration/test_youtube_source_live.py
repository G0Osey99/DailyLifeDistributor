from datetime import date, timedelta

import pytest

from tests.integration._creds_helpers import safely_has_credential

pytestmark = pytest.mark.integration


def _have_yt_auth() -> bool:
    """YouTube OAuth lives in the encrypted secrets store post-migration.

    The legacy on-disk ``token.json`` / ``client_secrets.json`` files are
    shredded by ``scripts/migrate_secrets.py`` on first boot, so checking
    the filesystem always returns False even when creds are present. The
    helper returns False on CI / fresh installs where the DB is unmigrated
    (no ``secrets`` table), where ``SECRET_ENC_KEY`` is missing, or where
    decryption otherwise fails — so the test skips cleanly instead of
    erroring at collection time.
    """
    return safely_has_credential("youtube.token", "youtube.client_secrets")


@pytest.mark.skipif(not _have_yt_auth(), reason="YouTube OAuth secrets missing")
def test_fetch_returns_well_formed_items():
    from core.refresh import youtube_source
    today = date.today()
    items = youtube_source.fetch(today - timedelta(days=30), today + timedelta(days=30))
    assert isinstance(items, list)
    for it in items:
        assert it.platform in ("youtube_video", "youtube_shorts")
        assert it.external_id
        assert it.iso_date
        assert it.status in ("scheduled", "published")
