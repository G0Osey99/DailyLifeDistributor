from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.integration


def _have_yt_auth() -> bool:
    """YouTube OAuth lives in the encrypted secrets store post-migration.

    The legacy on-disk ``token.json`` / ``client_secrets.json`` files are
    shredded by ``scripts/migrate_secrets.py`` on first boot, so checking the
    filesystem always returns False even when creds are present.

    Also verifies decryption works (SECRET_ENC_KEY is set + matches what
    the stored secrets were written with). Without the master key the
    rows are present but unreadable; we'd rather skip than die on a
    MasterKeyError mid-test.
    """
    try:
        from core import secrets_store
    except Exception:
        return False
    if not (
        secrets_store.has_secret("youtube.token")
        and secrets_store.has_secret("youtube.client_secrets")
    ):
        return False
    try:
        return (
            secrets_store.get_secret("youtube.token") is not None
            and secrets_store.get_blob("youtube.client_secrets") is not None
        )
    except Exception:
        return False


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
