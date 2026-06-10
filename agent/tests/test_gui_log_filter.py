"""filter_log_lines drives the Activity-log filter chips (All / Issues /
Success / Working) so a non-technical operator can isolate failures or
successes without reading the whole log."""
from __future__ import annotations

import pytest

# The GUI module pulls customtkinter/tkinter; skip cleanly where unavailable.
gui = pytest.importorskip("agent.gui")

LINES = [
    "INFO: run_batch._run_one finished platform=Rock success=True",        # ok
    "INFO: ... finished platform=Rock Email success=False error=no URL",   # err
    "WARNING: media never appeared - proceeding anyway",                   # warn
    "INFO:   Upload progress: 42%",                                        # busy
    "INFO: agent: PING control frame sent (idle 15.0s)",                   # dim
]


def _tags(mode):
    return [tag for _raw, tag, _emoji in gui.filter_log_lines(LINES, mode)]


def test_all_mode_keeps_everything():
    assert len(gui.filter_log_lines(LINES, "All")) == len(LINES)


def test_issues_mode_keeps_errors_and_warnings_only():
    assert _tags("Issues") == ["err", "warn"]


def test_success_mode_keeps_ok_only():
    assert _tags("Success") == ["ok"]


def test_working_mode_keeps_busy_only():
    assert _tags("Working") == ["busy"]


def test_unknown_mode_behaves_like_all():
    """A stale/renamed chip label must never blank the log."""
    assert len(gui.filter_log_lines(LINES, "definitely-not-a-mode")) == len(LINES)


def test_entries_carry_raw_line_and_emoji():
    raw, tag, emoji = gui.filter_log_lines(LINES, "Issues")[0]
    assert "success=False" in raw
    assert tag == "err"
    assert emoji.strip()  # the glyph prefix the render prepends


def test_filter_modes_cover_every_renderable_tag():
    """Every tag classify_log_line can emit is either reachable through a
    non-All chip or intentionally noise (dim/info shown only under All)."""
    chip_tags = set()
    for mode, allowed in gui.LOG_FILTERS.items():
        if allowed:
            chip_tags |= allowed
    assert chip_tags == {"err", "warn", "ok", "busy"}
