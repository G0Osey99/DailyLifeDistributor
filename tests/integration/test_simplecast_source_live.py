import os
from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not os.path.exists("simplecast_session.json"),
                    reason="SimpleCast session missing")
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
