"""Filesystem helpers for the agent release directory.

Releases live in /data/releases (bind-mounted from the host in the hosted
deploy; configurable via DLD_RELEASES_DIR for local/dev). The manifest is a
JSON file; binaries are named like `dld-agent-windows-0.1.0.exe`.

Path-traversal guard: only the manifest and binaries whose basename matches
[A-Za-z0-9._-]+ may be served.
"""
from __future__ import annotations

import os
import re

_NAME_OK = re.compile(r"^[A-Za-z0-9._-]+$")


def releases_dir() -> str:
    return os.environ.get("DLD_RELEASES_DIR", "/data/releases")


def manifest_path() -> str:
    return os.path.join(releases_dir(), "manifest.json")


def binary_path(filename: str) -> str | None:
    """Resolve a binary filename inside the releases dir, or None if invalid."""
    if not _NAME_OK.fullmatch(filename or ""):
        return None
    p = os.path.join(releases_dir(), filename)
    # Defense-in-depth: ensure the resolved path is still inside releases_dir.
    rd = os.path.realpath(releases_dir())
    rp = os.path.realpath(p)
    if not rp.startswith(rd + os.sep):
        return None
    return p
