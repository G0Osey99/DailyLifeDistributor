import os
from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not os.path.exists("rock_session.json"),
                    reason="Rock session missing")
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
