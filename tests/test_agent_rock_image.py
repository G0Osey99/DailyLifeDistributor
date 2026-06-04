"""Server-side Rock image pre-gather → agent rehydrate round-trip.

The hybrid agent has no local LLM, so the server gathers the Rock Vista
background image at dispatch time and ships it (base64 + credit metadata) in
the job plan; the agent rehydrates it into a GatheredImage. These tests cover
the gating (only when Rock+Vista+image apply) and the byte-exact round-trip.
"""
from __future__ import annotations

import base64
import os
import tempfile
from types import SimpleNamespace

from core import agent_dispatch
from core.image_gatherer import GatheredImage
from agent import run_batch


def _entry(scripture="Give your burdens to the Lord", date="2026-06-05"):
    return SimpleNamespace(scripture=scripture, date=date, topic_hint="")


_FULL_ELEMENTS = {"rock_vista": True, "rock_image": True}


def test_gather_skips_when_rock_not_in_platforms(monkeypatch):
    monkeypatch.setattr(agent_dispatch._img, "gather_image_for_verse",
                        lambda *a, **k: pytest_fail())
    assert agent_dispatch._gather_rock_image_for_agent(
        _entry(), _FULL_ELEMENTS, ["YouTube Video"]) is None


def test_gather_skips_when_image_element_off():
    assert agent_dispatch._gather_rock_image_for_agent(
        _entry(), {"rock_vista": True, "rock_image": False}, ["Rock"]) is None


def test_gather_skips_when_no_scripture():
    assert agent_dispatch._gather_rock_image_for_agent(
        _entry(scripture=""), _FULL_ELEMENTS, ["Rock"]) is None


def test_gather_returns_none_when_gatherer_finds_nothing(monkeypatch):
    monkeypatch.setattr(agent_dispatch._img, "gather_image_for_verse",
                        lambda *a, **k: None)
    assert agent_dispatch._gather_rock_image_for_agent(
        _entry(), _FULL_ELEMENTS, ["Rock"]) is None


def test_gather_then_rehydrate_round_trip(monkeypatch):
    raw = b"\xff\xd8\xff\xe0JFIF-fake-image-bytes"
    fd, srvpath = tempfile.mkstemp(suffix=".jpg")
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)

    gi = GatheredImage(file_path=srvpath, photo_id="p1", source="unsplash",
                       topic="still water", photographer="Jane Doe",
                       photo_url="https://unsplash.com/p/1")
    monkeypatch.setattr(agent_dispatch._img, "gather_image_for_verse",
                        lambda *a, **k: gi)

    payload = agent_dispatch._gather_rock_image_for_agent(
        _entry(), _FULL_ELEMENTS, ["Rock", "YouTube Video"])
    assert payload is not None
    assert payload["photo_id"] == "p1"
    assert payload["source"] == "unsplash"
    assert payload["topic"] == "still water"
    assert payload["photographer"] == "Jane Doe"
    assert base64.b64decode(payload["image_b64"]) == raw
    # The server's temp download is cleaned up after encoding.
    assert not os.path.exists(srvpath)

    # Agent side: rehydrate to a GatheredImage whose temp file has the bytes.
    rebuilt = run_batch._rehydrate_rock_image(payload)
    try:
        assert isinstance(rebuilt, GatheredImage)
        assert rebuilt.photo_id == "p1"
        assert rebuilt.topic == "still water"
        assert rebuilt.photo_url == "https://unsplash.com/p/1"
        with open(rebuilt.file_path, "rb") as fh:
            assert fh.read() == raw
    finally:
        if rebuilt and os.path.exists(rebuilt.file_path):
            os.unlink(rebuilt.file_path)


def test_rehydrate_none_on_missing_or_malformed():
    assert run_batch._rehydrate_rock_image(None) is None
    assert run_batch._rehydrate_rock_image({}) is None
    assert run_batch._rehydrate_rock_image({"photo_id": "x"}) is None  # no image_b64


def pytest_fail():  # helper: a lambda that must never be called
    raise AssertionError("gather_image_for_verse should not have been called")
