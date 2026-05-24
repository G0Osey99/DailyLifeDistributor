"""Schema: audit_log + audit_log_archive carry acting_as_org_id."""
from __future__ import annotations

from core import db as _db


def _columns(table: str) -> set[str]:
    with _db._get_conn() as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info('{table}')").fetchall()}


def test_audit_log_has_acting_as_org_id():
    assert "acting_as_org_id" in _columns("audit_log")


def test_audit_log_archive_has_acting_as_org_id():
    assert "acting_as_org_id" in _columns("audit_log_archive")
