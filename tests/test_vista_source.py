"""Guard test for core.refresh.vista_source.

PlaywrightSession deletes the on-disk session file on exit and re-materializes
it from the encrypted store on enter, so the fetch guard must check the store
(has_session), not the file — otherwise vista refresh works only once per
container start.
"""
from datetime import date

import pytest

from core.calendar_refresh import SessionExpiredError
from core.refresh import vista_source as v


def test_fetch_guard_checks_store_not_file(monkeypatch):
    monkeypatch.setattr(v, "has_session", lambda *a, **k: False)
    with pytest.raises(SessionExpiredError):
        v.fetch(date(2026, 1, 1), date(2026, 12, 31))
