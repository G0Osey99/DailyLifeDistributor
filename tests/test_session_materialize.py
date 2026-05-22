"""materialize_known_sessions restores session blobs from the encrypted store.

This is what keeps browser logins alive across a container rebuild (which wipes
the materialized *_session.json files under /app). The store round-trip itself
is covered elsewhere; here we pin the restore-loop behavior (skip existing
files, count restores, write to the project root).
"""
from core import playwright_session as pws, config as cfg


def test_restores_only_blobs_that_exist_in_store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "PROJECT_ROOT", str(tmp_path))

    def fake_load(path):
        # Pretend the store has every session except vista.
        if path.endswith("vista_social_session.json"):
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write("restored")
        return True

    monkeypatch.setattr(pws, "_load_session_blob_to", fake_load)

    restored = pws.materialize_known_sessions()

    assert restored == 2  # simplecast + rock, not vista
    assert (tmp_path / "rock_session.json").exists()
    assert not (tmp_path / "vista_social_session.json").exists()


def test_skips_files_that_already_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "rock_session.json").write_text("KEEP-ME")

    attempted: list[str] = []
    monkeypatch.setattr(pws, "_load_session_blob_to",
                        lambda p: attempted.append(p) or False)

    pws.materialize_known_sessions()

    # The present file is left untouched and not even re-loaded.
    assert (tmp_path / "rock_session.json").read_text() == "KEEP-ME"
    assert not any(p.endswith("rock_session.json") for p in attempted)


def test_no_stored_blobs_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(pws, "_load_session_blob_to", lambda p: False)
    assert pws.materialize_known_sessions() == 0
