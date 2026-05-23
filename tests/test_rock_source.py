"""Unit tests for core.refresh.rock_source — guard + page scrape transform.

The on-disk plaintext ``rock_session.json`` is shredded by the secrets
migration on first boot, so the fetch guard must consult the encrypted store
(``has_session``) and the runner must drive Playwright through
``PlaywrightSession`` (which materialises the blob to a tempfile internally),
not via a direct on-disk ``storage_state=`` load.
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.calendar_refresh import SessionExpiredError
from core.refresh import rock_source as r


def test_fetch_guard_checks_store_not_file(monkeypatch):
    """Guard reads has_session (which checks the encrypted store) — not the
    legacy ``_SESSION_FILE.exists()``. Without this, a freshly-migrated box
    raises SessionExpiredError even though the session is fine."""
    monkeypatch.setattr(r, "has_session", lambda *a, **k: False)
    with pytest.raises(SessionExpiredError):
        r.fetch(date(2026, 1, 1), date(2026, 12, 31))


def test_fetch_returns_empty_when_no_guids_configured(monkeypatch):
    """If config.yaml has no rock_channel_guids, fetch is a no-op (the
    Playwright session is never opened — we don't pay Chrome's launch cost
    just to discover there's nothing to scrape)."""
    monkeypatch.setattr(r, "has_session", lambda *a, **k: True)
    monkeypatch.setattr(r, "_channel_guids", lambda: [])

    called = {"playwright": False}

    class _StubSession:
        def __init__(self, *a, **k):
            called["playwright"] = True

        def __enter__(self):
            called["playwright"] = True
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(r, "PlaywrightSession", _StubSession)
    assert r.fetch(date(2026, 1, 1), date(2026, 12, 31)) == []
    assert called["playwright"] is False


_ROW = {"id": "12345", "cells": ["My Title", "thumb", "5/22/2026"]}


def test_scrape_channel_emits_items_for_in_window_rows(monkeypatch):
    """The split-out _scrape_channel helper produces ExternalItems for rows
    that fall inside the window — independent of PlaywrightSession's
    lifecycle so the navigation path stays unit-testable."""
    page = MagicMock()
    page.url = "https://rock.lcbcchurch.com/page/343?ContentChannelGuid=x"
    page.evaluate = MagicMock(return_value=[_ROW])
    page.wait_for_selector = MagicMock()
    page.wait_for_timeout = MagicMock()
    page.goto = MagicMock()

    out = r._scrape_channel(page, "guid-1",
                            date(2026, 1, 1), date(2026, 12, 31))
    assert len(out) == 1
    item = out[0]
    assert item.platform == "rock"
    assert item.external_id == "12345"
    assert item.iso_date == "2026-05-22"
    assert item.title == "My Title"
    assert item.status == "active"


def test_scrape_channel_raises_on_login_redirect(monkeypatch):
    """If the saved session has expired and we land on Rock's login page,
    raise SessionExpiredError immediately rather than scraping an empty grid
    and reporting a healthy 0-item refresh."""
    page = MagicMock()
    # Rock's login page is /page/3 — matches _LOGIN_PAGE_RE.
    page.url = "https://rock.lcbcchurch.com/page/3"
    page.goto = MagicMock()

    with pytest.raises(SessionExpiredError):
        r._scrape_channel(page, "guid-1",
                          date(2026, 1, 1), date(2026, 12, 31))


def test_scrape_channel_filters_out_of_window():
    """Dates outside the window are dropped silently — no ExternalItem
    emitted, the row just doesn't appear."""
    page = MagicMock()
    page.url = "https://rock.lcbcchurch.com/page/343"
    page.evaluate = MagicMock(return_value=[_ROW])
    page.wait_for_selector = MagicMock()
    page.wait_for_timeout = MagicMock()
    page.goto = MagicMock()

    # Window ends before the row's date (2026-05-22).
    assert r._scrape_channel(
        page, "guid-1", date(2026, 1, 1), date(2026, 1, 31)) == []


def test_fetch_uses_playwright_session(monkeypatch):
    """End-to-end fetch wires a SessionConfig + PlaywrightSession.

    We don't care which guids the operator has configured for this assertion
    — just that fetch goes through PlaywrightSession (the temp-file
    materialisation path) rather than opening rock_session.json directly.
    """
    monkeypatch.setattr(r, "has_session", lambda *a, **k: True)
    monkeypatch.setattr(r, "_channel_guids", lambda: ["guid-A"])

    # Fake PlaywrightSession that yields a fake page.
    fake_page = MagicMock()
    fake_page.url = "https://rock.lcbcchurch.com/page/343?ContentChannelGuid=guid-A"
    fake_page.evaluate = MagicMock(return_value=[])
    fake_page.wait_for_selector = MagicMock()
    fake_page.wait_for_timeout = MagicMock()
    fake_page.goto = MagicMock()

    used = {}

    class _FakeSession:
        def __init__(self, cfg, **kw):
            used["cfg"] = cfg

        def __enter__(self):
            return SimpleNamespace(page=fake_page)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(r, "PlaywrightSession", _FakeSession)
    out = r.fetch(date(2026, 1, 1), date(2026, 12, 31))
    assert out == []
    cfg = used["cfg"]
    assert cfg.name == "rock"
    # The cfg's session_file points at the project-root rock_session.json
    # path — PlaywrightSession then materialises the encrypted blob to that
    # path on enter and shreds it on exit.
    assert cfg.session_file.endswith("rock_session.json")
    assert cfg.no_login_recovery is True
