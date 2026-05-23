import json
from agent import config


class _MemKeyring:
    def __init__(self): self.store = {}
    def set_password(self, svc, user, pw): self.store[(svc, user)] = pw
    def get_password(self, svc, user): return self.store.get((svc, user))
    def delete_password(self, svc, user): self.store.pop((svc, user), None)


def test_token_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_keyring", _MemKeyring())
    monkeypatch.setattr(config, "_CONFIG_PATH", str(tmp_path / "agent.json"))
    config.set_token("abc123")
    assert config.get_token() == "abc123"


def test_config_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_CONFIG_PATH", str(tmp_path / "agent.json"))
    config.set_server_url("https://autoalert.pro")
    config.set_media_roots({"video": "/Users/x/vids"})
    assert config.get_server_url() == "https://autoalert.pro"
    assert config.get_media_roots() == {"video": "/Users/x/vids"}
    data = json.load(open(tmp_path / "agent.json"))
    assert data["server_url"] == "https://autoalert.pro"


def test_clear_token_swallows_backend_errors_with_debug_log(monkeypatch, caplog):
    """clear_token must not raise when keyring's backend throws (e.g.
    PasswordDeleteError on first run, or transient backend hiccups).
    The failure is logged at debug so triage can spot real backend
    issues without scaring users on the happy path."""
    class _Raising(_MemKeyring):
        def delete_password(self, svc, user):
            raise RuntimeError("simulated backend failure")

    monkeypatch.setattr(config, "_keyring", _Raising())
    with caplog.at_level("DEBUG", logger="agent.config"):
        # Must not raise.
        config.clear_token()
    debug_lines = [r for r in caplog.records
                   if r.levelname == "DEBUG"
                   and "clear_token" in r.message]
    assert debug_lines, "expected a DEBUG log line on backend failure"
