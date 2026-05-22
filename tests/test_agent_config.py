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
