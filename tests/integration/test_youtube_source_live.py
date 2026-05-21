import os
from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.integration


def _have_yt_auth() -> bool:
    return os.path.exists("token.json") and os.path.exists("client_secrets.json")


@pytest.mark.skipif(not _have_yt_auth(), reason="YouTube OAuth files missing")
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
