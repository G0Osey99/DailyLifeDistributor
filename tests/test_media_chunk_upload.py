"""Chunked upload + reassembly: init run, allocate file-id, append chunks."""
import io

import pytest

from core import auth, media_session as ms


@pytest.fixture()
def client(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    # Reset the module-level run lock / active-run map between tests (the
    # blueprint module is a process singleton).
    from blueprints import media
    media._run_lock = ms.RunLock()
    media._runs.clear()
    auth.reset_lockouts()
    auth.set_password("pw")
    import app as flask_app_module
    flask_app_module.app.config["TESTING"] = True
    with flask_app_module.app.test_client() as c:
        c.post("/login", data={"password": "pw"})
        yield c


def _init_run(client):
    resp = client.post("/media/run/init", json={})
    assert resp.status_code == 200
    return resp.get_json()["run_id"]


def _new_file(client, run_id):
    resp = client.post(f"/media/file/new?run_id={run_id}")
    assert resp.status_code == 200
    return resp.get_json()["file_id"]


def _send_chunk(client, run_id, file_id, idx, total, data):
    return client.post(
        "/media/upload/chunk",
        data={
            "run_id": run_id,
            "file_id": file_id,
            "chunk_index": str(idx),
            "total_chunks": str(total),
            "data": (io.BytesIO(data), "blob"),
        },
        content_type="multipart/form-data",
    )


def test_chunks_reassemble_to_payload(client, tmp_path):
    run_id = _init_run(client)
    file_id = _new_file(client, run_id)
    parts = [b"AAAA", b"BBBB", b"CCDD"]
    payload = b"".join(parts)
    last = None
    for i, p in enumerate(parts):
        last = _send_chunk(client, run_id, file_id, i, len(parts), p)
        assert last.status_code == 200
    body = last.get_json()
    assert body["complete"] is True
    assert body["bytes"] == len(payload)

    from blueprints import media
    path = media._runs[run_id]["dir"].file_path(file_id)
    with open(path, "rb") as fh:
        assert fh.read() == payload


def test_bad_file_id_rejected(client):
    run_id = _init_run(client)
    resp = _send_chunk(client, run_id, "not-a-real-id", 0, 1, b"x")
    assert resp.status_code == 400


def test_chunk_over_cap_rejected(client, monkeypatch):
    from blueprints import media
    monkeypatch.setattr(media, "_MAX_CHUNK", 4)
    run_id = _init_run(client)
    file_id = _new_file(client, run_id)
    resp = _send_chunk(client, run_id, file_id, 0, 1, b"too many bytes")
    assert resp.status_code == 413


def test_chunk_without_active_run_rejected(client):
    resp = _send_chunk(client, "no-such-run", "abc", 0, 1, b"x")
    assert resp.status_code == 409


def test_second_run_init_is_busy(client):
    _init_run(client)
    resp = client.post("/media/run/init", json={})
    assert resp.status_code == 409
