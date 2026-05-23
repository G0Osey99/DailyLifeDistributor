# agent/tests/test_secrets_shim.py
import json, os, sys, tempfile
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


# ---------------------------------------------------------------------------
# Phase 3 — has_secret / get_blob / set_blob (Playwright uploader surface)
# ---------------------------------------------------------------------------


def test_has_secret_true_for_known_false_for_missing():
    s = secrets_shim.Shim(initial={"a.k": "v"})
    assert s.has_secret("a.k") is True
    assert s.has_secret("nope") is False


def test_get_blob_returns_utf8_bytes_of_value():
    s = secrets_shim.Shim(initial={"playwright.simplecast_session": '{"cookies":[]}'})
    blob = s.get_blob("playwright.simplecast_session")
    assert isinstance(blob, bytes)
    assert blob == b'{"cookies":[]}'


def test_get_blob_returns_none_when_missing():
    s = secrets_shim.Shim(initial={})
    assert s.get_blob("missing") is None


def test_set_blob_roundtrips_bytes_to_string_and_emits():
    emitted = []
    s = secrets_shim.Shim(initial={}, emit=emitted.append)
    s.set_blob("playwright.rock_session", b'{"cookies":[{"name":"x"}]}')
    # Stored as str
    assert s.get_secret("playwright.rock_session") == '{"cookies":[{"name":"x"}]}'
    # Round-trip back through get_blob
    assert s.get_blob("playwright.rock_session") == b'{"cookies":[{"name":"x"}]}'
    # Emitted exactly one credentials_updated frame
    assert len(emitted) == 1
    assert emitted[0]["type"] == "credentials_updated"
    assert emitted[0]["key"] == "playwright.rock_session"
    assert emitted[0]["value"] == '{"cookies":[{"name":"x"}]}'


def test_set_blob_drops_non_utf8_silently_with_log():
    emitted = []
    s = secrets_shim.Shim(initial={}, emit=emitted.append)
    # Arbitrary binary that isn't valid UTF-8
    s.set_blob("k", b"\xff\xfe\xfd")
    assert s.has_secret("k") is False
    assert emitted == []


def test_install_exposes_all_blob_and_has_methods_on_synthetic_module():
    """install_as_core_secrets_store wires has_secret/get_blob/set_blob onto
    the synthetic core.secrets_store module so Playwright uploaders work."""
    emitted = []
    shim = secrets_shim.install_as_core_secrets_store(
        initial={"playwright.vista_social_session": '{"v":1}'},
        emit=emitted.append,
    )
    try:
        mod = sys.modules["core.secrets_store"]
        # has_secret on the module
        assert mod.has_secret("playwright.vista_social_session") is True
        assert mod.has_secret("none") is False
        # get_blob on the module
        assert mod.get_blob("playwright.vista_social_session") == b'{"v":1}'
        # set_blob on the module (round-trips + emits)
        mod.set_blob("playwright.vista_social_session", b'{"v":2}')
        assert mod.get_blob("playwright.vista_social_session") == b'{"v":2}'
        assert any(e.get("type") == "credentials_updated" for e in emitted)
    finally:
        sys.modules.pop("core.secrets_store", None)
