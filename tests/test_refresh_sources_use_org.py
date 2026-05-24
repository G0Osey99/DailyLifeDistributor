"""Calendar refresh sources pass effective_org_id to has_session.

Without this, the source short-circuits with SessionExpiredError because
has_session(path) without org_id reads the empty legacy unscoped slot —
the symptom the user reported when YouTube refreshed but Rock /
SimpleCast / Vista all reported "session expired" while the sidebar
showed them green.

These tests pin the wiring at the source level by mocking has_session
to record the org_id kwarg it received.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from core import org_context


def _date_range():
    return date(2026, 5, 1), date(2026, 5, 31)


def test_rock_source_passes_org_to_has_session():
    from core.refresh import rock_source
    seen: dict = {}

    def fake_has_session(path, *, org_id=None):
        seen["org_id"] = org_id
        return False  # short-circuit; we only care about the kwarg

    with patch("core.refresh.rock_source.has_session", side_effect=fake_has_session):
        with org_context.override(42):
            with pytest.raises(Exception):  # SessionExpiredError or similar
                rock_source.fetch(*_date_range())
    assert seen["org_id"] == 42


def test_simplecast_source_passes_org_to_has_session():
    from core.refresh import simplecast_source
    seen: dict = {}

    def fake_has_session(path, *, org_id=None):
        seen["org_id"] = org_id
        return False

    with patch("core.refresh.simplecast_source.has_session", side_effect=fake_has_session):
        with org_context.override(7):
            with pytest.raises(Exception):
                simplecast_source.fetch(*_date_range())
    assert seen["org_id"] == 7


def test_vista_source_passes_org_to_has_session():
    from core.refresh import vista_source
    seen: dict = {}

    def fake_has_session(path, *, org_id=None):
        seen["org_id"] = org_id
        return False

    with patch("core.refresh.vista_source.has_session", side_effect=fake_has_session):
        with org_context.override(11):
            with pytest.raises(Exception):
                vista_source.fetch(*_date_range())
    assert seen["org_id"] == 11


def test_rock_email_source_passes_org_to_has_session():
    from core.refresh import rock_email_source
    seen: dict = {}

    def fake_has_session(path, *, org_id=None):
        seen["org_id"] = org_id
        return False

    with patch("core.refresh.rock_email_source.has_session", side_effect=fake_has_session):
        with org_context.override(99):
            with pytest.raises(Exception):
                rock_email_source.fetch(*_date_range())
    assert seen["org_id"] == 99


def test_source_with_no_override_returns_none_org():
    """Bare call (no override, no Flask context) → org_id=None."""
    from core.refresh import rock_source
    seen: dict = {}

    def fake_has_session(path, *, org_id=None):
        seen["org_id"] = org_id
        return False

    with patch("core.refresh.rock_source.has_session", side_effect=fake_has_session):
        with pytest.raises(Exception):
            rock_source.fetch(*_date_range())
    assert seen["org_id"] is None
