"""Per-session settings + run-state for the browser-streaming pipeline."""
import os
from core import media_session as ms


def test_temp_root_under_data(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    run = ms.RunDir.allocate()
    assert os.path.isdir(run.path)
    assert run.path.startswith(str(tmp_path / "uploads"))
    run.cleanup()
    assert not os.path.exists(run.path)


def test_file_path_is_namespaced(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    run = ms.RunDir.allocate()
    fid = run.new_file_id()                 # server-issued uuid
    p = run.file_path(fid)
    # Must stay inside the run dir regardless of anything.
    assert os.path.realpath(p).startswith(os.path.realpath(run.path))
    run.cleanup()


def test_run_lock_single_holder(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path / "uploads"))
    lock = ms.RunLock()
    assert lock.acquire("run-a") is True
    assert lock.acquire("run-b") is False   # already held
    lock.release("run-a")
    assert lock.acquire("run-b") is True
    lock.release("run-b")


def test_orphan_sweep_removes_stale(monkeypatch, tmp_path):
    root = tmp_path / "uploads"
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(root))
    run = ms.RunDir.allocate()
    # Simulate an orphan: a run dir with no active lock.
    ms.sweep_orphans(active_run_ids=set())
    assert not os.path.exists(run.path)


def test_free_space_check(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "_TEMP_ROOT", str(tmp_path))
    # 0 required always fits; an absurd requirement never does.
    assert ms.has_free_space(0) is True
    assert ms.has_free_space(10**18) is False
