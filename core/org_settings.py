"""Per-organization configuration overlay.

Stored sections (today): ``scheduling``, ``description_footers``. Each
section is a small JSON blob that overrides the same-named subtree of
``config.yaml`` when looked up via ``core.config.effective_config(org_id)``.

The underlying table is ``org_settings(org_id, section, value_json,
updated_at)``. Reads on a missing section return ``None``; reads on a
present-but-stale-key section return the empty dict — callers are expected
to merge whatever's there onto the global default themselves (via
``effective_config``), not to overwrite.

Settings page uses these helpers directly; uploaders and ``session_state``
read through ``effective_config`` so they don't need to know there's an
overlay.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.db import _get_conn

_log = logging.getLogger(__name__)


def get_section(org_id: int, section: str) -> dict | None:
    """Return the stored dict for *(org_id, section)*, or None if unset.

    A returned dict reflects ONLY this org's overrides. Callers that need
    the org's-overrides-merged-on-the-global-default should use
    ``core.config.effective_config(org_id)`` instead.
    """
    if org_id is None:
        return None
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value_json FROM org_settings WHERE org_id = ? AND section = ?",
            (int(org_id), section),
        ).fetchone()
    if row is None:
        return None
    try:
        v = json.loads(row["value_json"])
        # A row whose JSON parses to something non-dict (e.g. legacy bad
        # data) is treated as missing so we never feed a non-dict back to
        # callers that expect a mergeable shape.
        return v if isinstance(v, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        _log.warning(
            "org_settings(%s, %s): JSON decode failed; treating as unset",
            org_id, section,
        )
        return None


def set_section(org_id: int, section: str, value: dict) -> None:
    """Upsert the *(org_id, section)* row with *value* (must be a dict).

    Pass an empty dict to clear the override (the row is still present but
    contains ``{}``; downstream merges become no-ops). To fully remove the
    row, use ``delete_section``.
    """
    if not isinstance(value, dict):
        raise ValueError(
            f"org_settings.set_section expects dict, got {type(value).__name__}"
        )
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO org_settings (org_id, section, value_json, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(org_id, section) DO UPDATE SET "
            "value_json = excluded.value_json, "
            "updated_at = excluded.updated_at",
            (int(org_id), section, json.dumps(value), now),
        )
        conn.commit()


def delete_section(org_id: int, section: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM org_settings WHERE org_id = ? AND section = ?",
            (int(org_id), section),
        )
        conn.commit()
