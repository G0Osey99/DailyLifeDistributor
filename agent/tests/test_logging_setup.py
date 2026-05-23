"""Tests for agent/main.configure_logging — Phase 3 hardening.

Covers:
  1. File handler is registered and the log file is created in the dir.
  2. --log-dir overrides the default (we pass log_dir= directly here).
  3. --verbose elevates BOTH the file AND the stdout handler to DEBUG.
  4. Non-verbose: file=INFO, stdout=WARNING (so the file is more verbose
     than the console — diagnostic trail intact even when console is quiet).
  5. Repeated calls don't accumulate handlers (idempotent reset).
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _restore_logging():
    """Snapshot + restore root logger state so we don't pollute other tests."""
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def test_configure_logging_creates_log_dir_and_file(tmp_path):
    """The directory is mkdir'd and the file handler points to agent.log."""
    from agent.main import configure_logging

    target = tmp_path / "logs"
    assert not target.exists()  # configure_logging must create it.

    resolved = configure_logging(log_dir=str(target), verbose=False)

    assert resolved == target
    assert target.is_dir()

    # The RotatingFileHandler we attached must point at <target>/agent.log.
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    fh = file_handlers[0]
    assert Path(fh.baseFilename) == target / "agent.log"

    # And it's a *Rotating* file handler with our cap.
    assert fh.maxBytes == 10 * 1024 * 1024
    assert fh.backupCount == 5


def test_configure_logging_log_dir_override(tmp_path):
    """An explicit --log-dir overrides the platformdirs/home default."""
    from agent.main import configure_logging

    custom = tmp_path / "custom-spot" / "deeper"
    resolved = configure_logging(log_dir=str(custom), verbose=False)
    assert resolved == custom
    assert custom.is_dir()


def test_configure_logging_non_verbose_levels(tmp_path):
    """Default mode: file=INFO, stdout=WARNING."""
    from agent.main import configure_logging

    configure_logging(log_dir=str(tmp_path), verbose=False)

    root = logging.getLogger()
    fh = [h for h in root.handlers
          if isinstance(h, logging.handlers.RotatingFileHandler)][0]
    sh = [h for h in root.handlers
          if (type(h) is logging.StreamHandler)][0]

    assert fh.level == logging.INFO, (
        "file handler must default to INFO so we always have a diagnostic trail"
    )
    assert sh.level == logging.WARNING


def test_configure_logging_verbose_lifts_both_handlers(tmp_path):
    """--verbose lifts BOTH the file and the stdout handlers to DEBUG."""
    from agent.main import configure_logging

    configure_logging(log_dir=str(tmp_path), verbose=True)

    root = logging.getLogger()
    fh = [h for h in root.handlers
          if isinstance(h, logging.handlers.RotatingFileHandler)][0]
    sh = [h for h in root.handlers
          if (type(h) is logging.StreamHandler)][0]

    assert fh.level == logging.DEBUG
    assert sh.level == logging.DEBUG
    assert root.level == logging.DEBUG


def test_configure_logging_is_idempotent(tmp_path):
    """Calling twice doesn't accumulate duplicate handlers."""
    from agent.main import configure_logging

    configure_logging(log_dir=str(tmp_path), verbose=False)
    configure_logging(log_dir=str(tmp_path), verbose=False)

    root = logging.getLogger()
    # Exactly two handlers: RotatingFileHandler + StreamHandler.
    assert len(root.handlers) == 2


def test_log_messages_actually_land_in_the_file(tmp_path):
    """End-to-end: emit a record, then assert it's on disk."""
    from agent.main import configure_logging

    configure_logging(log_dir=str(tmp_path), verbose=False)

    logger = logging.getLogger("dld.agent.test")
    logger.info("hello world — file logging works")

    # Flush handlers so the bytes land before we read.
    for h in logging.getLogger().handlers:
        h.flush()

    log_file = tmp_path / "agent.log"
    assert log_file.is_file()
    content = log_file.read_text(encoding="utf-8")
    assert "hello world" in content
    assert "INFO" in content


def test_default_log_dir_returns_a_path():
    """The default log dir resolves to *something* — either platformdirs
    or the ~/.dld-agent/logs fallback. Importable without errors."""
    from agent.main import _default_log_dir

    result = _default_log_dir()
    assert isinstance(result, Path)
    # Must contain "dld" so it's namespaced (platformdirs convention or
    # our fallback both satisfy this).
    assert "dld" in str(result).lower()
