"""Per-session settings + per-run temp/lock management for the streaming pipeline.

Two scopes:
  * Per browser session (Flask session cookie): column mapping + selected dates.
  * Per process: a single run lock (one upload at a time) and per-run temp dirs
    that hold transiently-uploaded media, plus an orphan sweep.
"""
from __future__ import annotations

import os
import shutil
import threading
import uuid
from dataclasses import dataclass

# Temp media lives on the data volume (DLD mounts dld-data at /data on the VPS).
# Falls back to a repo-local dir for local/dev runs.
_TEMP_ROOT = os.environ.get("DLD_UPLOAD_TMP") or (
    "/data/uploads" if os.path.isdir("/data") else
    os.path.join(os.path.dirname(os.path.dirname(__file__)), ".uploads")
)


@dataclass
class RunDir:
    run_id: str
    path: str

    @classmethod
    def allocate(cls) -> "RunDir":
        run_id = uuid.uuid4().hex
        path = os.path.join(_TEMP_ROOT, run_id)
        os.makedirs(path, exist_ok=True)
        if os.name != "nt":
            os.chmod(path, 0o700)
        return cls(run_id=run_id, path=path)

    def new_file_id(self) -> str:
        return uuid.uuid4().hex

    def file_path(self, file_id: str) -> str:
        # file_id is server-issued (uuid hex); reject anything else so a crafted
        # value can't escape the run dir.
        if not file_id or any(c not in "0123456789abcdef" for c in file_id):
            raise ValueError("bad file_id")
        return os.path.join(self.path, file_id)

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


class RunLock:
    """One upload run at a time. Holder identified by run_id so the same run
    can re-enter / release; a different run is refused while one is active."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: str | None = None

    def acquire(self, run_id: str) -> bool:
        with self._lock:
            if self._holder is not None and self._holder != run_id:
                return False
            self._holder = run_id
            return True

    def release(self, run_id: str) -> None:
        with self._lock:
            if self._holder == run_id:
                self._holder = None

    def holder(self) -> str | None:
        with self._lock:
            return self._holder


def _is_run_id(name: str) -> bool:
    """A run id is a uuid4 hex (32 lowercase hex chars). Only these are swept,
    so sibling state under _TEMP_ROOT (e.g. the per-session spreadsheet cache)
    is never collaterally deleted."""
    return len(name) == 32 and all(c in "0123456789abcdef" for c in name)


def sweep_orphans(active_run_ids: set[str]) -> int:
    """Remove any temp *run* dir not in active_run_ids. Returns count removed.

    Scoped to run-id-named directories so the sweep can't wipe other state
    living under the temp root (the spreadsheet cache, etc.)."""
    removed = 0
    if not os.path.isdir(_TEMP_ROOT):
        return 0
    for name in os.listdir(_TEMP_ROOT):
        if name in active_run_ids or not _is_run_id(name):
            continue
        full = os.path.join(_TEMP_ROOT, name)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
            removed += 1
    return removed


def has_free_space(required_bytes: int) -> bool:
    """True if the temp volume can hold required_bytes with a safety margin."""
    os.makedirs(_TEMP_ROOT, exist_ok=True)
    usage = shutil.disk_usage(_TEMP_ROOT)
    margin = 2 * 1024 * 1024 * 1024  # keep 2 GB headroom
    return usage.free >= required_bytes + margin
