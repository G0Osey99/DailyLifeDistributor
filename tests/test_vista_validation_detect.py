"""Vista schedule-step network-validation detection + picker readiness.

When Instagram is still validating the just-uploaded Shorts video, Vista shows
a content toast and blocks "Next". _detect_network_validation_error recognises
that toast (so the retry loop knows to wait and re-click rather than fail),
and _picker_visible reports when the date step has finally mounted.
"""
from __future__ import annotations

from uploaders import vista_social_uploader as V


class _Loc:
    def __init__(self, count: int, visible: bool):
        self._count, self._visible = count, visible

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def is_visible(self):
        return self._visible


class _Page:
    def __init__(self, *, picker_count=0, picker_visible=False, toast=""):
        self._picker = _Loc(picker_count, picker_visible)
        self._toast = toast

    def locator(self, sel):
        assert sel == V._PICKER_SEL
        return self._picker

    def evaluate(self, _js):
        return self._toast


def test_picker_visible_true_only_when_present_and_visible():
    assert V._picker_visible(_Page(picker_count=1, picker_visible=True)) is True
    assert V._picker_visible(_Page(picker_count=1, picker_visible=False)) is False
    assert V._picker_visible(_Page(picker_count=0, picker_visible=False)) is False


def test_detect_matches_instagram_content_toast():
    page = _Page(toast="Please check your content on the following social networks: Instagram")
    out = V._detect_network_validation_error(page)
    assert "Instagram" in out


def test_detect_matches_cant_be_scheduled_phrasing():
    page = _Page(toast="This post can't be scheduled")
    assert V._detect_network_validation_error(page) == "This post can't be scheduled"


def test_detect_returns_empty_when_no_toast():
    assert V._detect_network_validation_error(_Page(toast="")) == ""


def test_detect_swallows_evaluate_errors():
    class _Boom(_Page):
        def evaluate(self, _js):
            raise RuntimeError("page gone")

    assert V._detect_network_validation_error(_Boom()) == ""
