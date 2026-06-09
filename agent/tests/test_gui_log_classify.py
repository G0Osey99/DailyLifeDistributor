"""classify_log_line maps agent-log lines to (colour-tag, emoji) so the GUI
activity panel is scannable for a non-technical operator."""
from __future__ import annotations

import pytest

# The GUI module pulls customtkinter/tkinter; skip cleanly where unavailable.
gui = pytest.importorskip("agent.gui")
classify = gui.classify_log_line


@pytest.mark.parametrize("line,tag", [
    ("INFO: run_batch._run_one finished platform=YouTube Video success=True", "ok"),
    ("INFO: SimpleCast: scheduled for 2026-06-10 00:00:00-04:00", "ok"),
    ("INFO: Rock: Linked child item 18465 -> parent 18466", "ok"),
    ("INFO: ... finished platform=Rock Email success=False error=no watch URL", "err"),
    ("ERROR core.image_gatherer: Image gatherer: llamafile is not running", "err"),
    ("WARNING: media-ready signal never appeared after 300 s - proceeding anyway", "warn"),
    ("INFO:   Upload progress: 42%", "busy"),
    ("INFO: Uploading video: The Small Things", "busy"),
    ("INFO: agent: PING control frame sent (idle 15.0s)", "dim"),
    ("INFO: sessions poll OK had_token=True", "dim"),
    ("INFO: agent: WebSocket connected as LCBC-PC", "dim"),
])
def test_classify_tag(line, tag):
    assert classify(line)[0] == tag


def test_every_line_gets_a_prefix_and_known_tag():
    valid = {"ok", "err", "warn", "busy", "dim", "info"}
    for line in ["", "something totally unexpected", "INFO: hello world"]:
        tag, emoji = classify(line)
        assert tag in valid
        assert isinstance(emoji, str) and emoji.strip("· ") is not None
