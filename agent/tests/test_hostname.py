"""Tests for agent.hostname — friendly-hostname normalization."""
from __future__ import annotations

import pytest

from agent import hostname


def test_returns_socket_hostname_by_default(monkeypatch):
    monkeypatch.setattr(hostname.socket, "gethostname", lambda: "Studio")
    assert hostname.get_friendly_hostname() == "Studio"


def test_strips_dot_local_macos(monkeypatch):
    monkeypatch.setattr(hostname.socket, "gethostname", lambda: "Studio.local")
    assert hostname.get_friendly_hostname() == "Studio"


def test_strips_dot_local_case_insensitive(monkeypatch):
    monkeypatch.setattr(hostname.socket, "gethostname", lambda: "Studio.LOCAL")
    assert hostname.get_friendly_hostname() == "Studio"


def test_strips_whitespace(monkeypatch):
    monkeypatch.setattr(hostname.socket, "gethostname", lambda: "  Studio  ")
    assert hostname.get_friendly_hostname() == "Studio"


def test_strips_whitespace_then_dot_local(monkeypatch):
    monkeypatch.setattr(hostname.socket, "gethostname", lambda: "  Studio.local  ")
    assert hostname.get_friendly_hostname() == "Studio"


def test_length_cap_64(monkeypatch):
    long_name = "x" * 200
    monkeypatch.setattr(hostname.socket, "gethostname", lambda: long_name)
    out = hostname.get_friendly_hostname()
    assert len(out) == 64
    assert out == "x" * 64


def test_empty_returns_device_fallback(monkeypatch):
    monkeypatch.setattr(hostname.socket, "gethostname", lambda: "")
    assert hostname.get_friendly_hostname() == "device"


def test_whitespace_only_returns_device_fallback(monkeypatch):
    monkeypatch.setattr(hostname.socket, "gethostname", lambda: "   ")
    assert hostname.get_friendly_hostname() == "device"


def test_dot_local_only_returns_device_fallback(monkeypatch):
    # ".local" alone strips to "" → fallback "device".
    monkeypatch.setattr(hostname.socket, "gethostname", lambda: ".local")
    assert hostname.get_friendly_hostname() == "device"


def test_returns_str():
    """The hostname must always be a non-empty str (no None)."""
    out = hostname.get_friendly_hostname()
    assert isinstance(out, str)
    assert len(out) > 0
