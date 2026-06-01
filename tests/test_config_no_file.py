"""core.config must import + load WITHOUT PyYAML or a config.yaml present.

Regression for the agent-dispatch crash: the bundled hybrid agent ships no
PyYAML and has no config.yaml on disk, but core.session_state instantiates its
SessionState singleton at import (which calls load_config()). A top-level
`import yaml` / unconditional file parse crashed every agent dispatch with
ModuleNotFoundError. load_config() must degrade to {} when there's no config
file, and must not need the parser in that case.
"""
from __future__ import annotations

import core.config as cfg


def test_load_config_returns_empty_when_file_absent(monkeypatch, tmp_path):
    """No config.yaml → {} (the agent's case), no crash, no parser needed."""
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "does-not-exist.yaml"))
    cfg.invalidate_config_cache()
    assert cfg.load_config() == {}
    cfg.invalidate_config_cache()


def test_load_config_parses_existing_file(monkeypatch, tmp_path):
    """config.yaml present → parsed normally (server path unchanged)."""
    p = tmp_path / "config.yaml"
    p.write_text("upload:\n  max_workers: 7\nplatforms:\n  youtube_video: true\n",
                 encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(p))
    cfg.invalidate_config_cache()
    data = cfg.load_config()
    assert data["upload"]["max_workers"] == 7
    assert data["platforms"]["youtube_video"] is True
    cfg.invalidate_config_cache()


def test_yaml_not_imported_at_module_top():
    """The module must not bind `yaml` at import time (it's lazy now), so the
    bundled agent without PyYAML can still import core.config."""
    assert not hasattr(cfg, "yaml"), "yaml must be imported lazily inside load_config"
