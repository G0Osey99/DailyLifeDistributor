# agent/tests/test_secrets_shim.py
import json, os, tempfile
import pytest
from agent import secrets_shim

def test_get_returns_value_seeded_from_envelope():
    s = secrets_shim.Shim(initial={"a.k": "v1"})
    assert s.get_secret("a.k") == "v1"
    assert s.get_secret("missing") is None

def test_set_emits_credentials_updated_and_overwrites():
    emitted = []
    s = secrets_shim.Shim(initial={}, emit=emitted.append)
    s.set_secret("youtube.token", "{}")
    s.set_secret("youtube.token", "{\"refreshed\":1}")
    assert s.get_secret("youtube.token") == "{\"refreshed\":1}"
    assert [e["key"] for e in emitted] == ["youtube.token", "youtube.token"]
    assert emitted[-1]["value"] == "{\"refreshed\":1}"
    assert emitted[-1]["type"] == "credentials_updated"

def test_delete_emits_credentials_updated_with_empty_value():
    emitted = []
    s = secrets_shim.Shim(initial={"k": "v"}, emit=emitted.append)
    s.delete_secret("k")
    assert s.get_secret("k") is None
    assert emitted[-1] == {"type": "credentials_updated", "key": "k", "value": ""}

def test_materialize_writes_to_tempfile_and_cleans_up():
    s = secrets_shim.Shim(initial={"yt.cs": '{"client":"x"}'})
    with s.materialize_blob_to_tempfile("yt.cs", suffix=".json") as path:
        assert path and os.path.exists(path)
        assert json.load(open(path)) == {"client": "x"}
        held = path
    assert not os.path.exists(held)

def test_materialize_returns_none_when_missing():
    s = secrets_shim.Shim(initial={})
    with s.materialize_blob_to_tempfile("missing.k") as path:
        assert path is None
