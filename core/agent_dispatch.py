"""Server-side dispatcher: builds a job_plan envelope for the agent path,
sends it through the relay, and ingests the result stream.

Mirrors core.upload_jobs.run_batch inputs but never calls uploaders
locally — execution happens on the paired agent. See
docs/superpowers/specs/2026-05-22-hybrid-upload-agent-phase3-design.md.
"""
from __future__ import annotations
import logging
from typing import Any
from core import db as _db

_PROTOCOL_VERSION = 1

# Path fields removed from a serialized ReviewEntry before send; the agent
# re-resolves them from its own scan map.
# Notes on the full list:
#   - youtube_video_path, youtube_shorts_path, podcast_path: primary media paths
#   - thumbnail_path: YouTube/SimpleCast thumbnail
#   - email_thumbnail_path: Rock email thumbnail (separate dir)
#   - spotlight_image_path, vista_image_path, reflection_image_path: planned
#     Rock image fields (not yet on ReviewEntry; harmless to include — stripped
#     only if present in the serialized dict)
_STRIPPED_PATH_FIELDS = frozenset((
    "youtube_video_path",
    "youtube_shorts_path",
    "podcast_path",
    "thumbnail_path",
    "email_thumbnail_path",
    "spotlight_image_path",
    "vista_image_path",
    "reflection_image_path",
))

_logger = logging.getLogger(__name__)


def _strip_paths(entry_dict: dict) -> dict:
    return {k: v for k, v in entry_dict.items() if k not in _STRIPPED_PATH_FIELDS}


def build_envelope(
    *,
    job_id: str,
    rows: list[dict],
    entries: dict,        # iso_date -> ReviewEntry
    credentials: dict,    # secrets_store key -> blob string
    config: dict,
) -> dict:
    """Compose the job_plan envelope. Pure function; no I/O."""
    out_rows = []
    for r in rows:
        iso = r["iso_date"]
        entry = entries[iso]
        out_rows.append({
            "row_idx": r["row_idx"],
            "iso_date": iso,
            "platforms": list(r["platforms"]),
            "elements": r["elements"],
            "entry": _strip_paths(entry.to_dict()),
        })
    return {
        "v": 1,
        "type": "job_plan",
        "job_id": job_id,
        "protocol_version": _PROTOCOL_VERSION,
        "config": config,
        "rows": out_rows,
        "credentials": dict(credentials),
    }


def filter_done_rows(*, session_id: str, summary: list[dict]) -> list[dict]:
    """Drop platforms (and entire rows) already recorded as ``success``
    in upload_history.

    Input: session_id, summary = list of {"date": iso, "platforms": [...]}
    Output: list of {"row_idx": idx_in_summary, "iso_date": iso,
    "platforms": [remaining]}, entire row omitted if all platforms done.
    """
    out: list[dict] = []
    for idx, item in enumerate(summary):
        iso = item["date"]
        remaining = [
            p for p in item["platforms"]
            if not _db.has_successful_upload(session_id, iso, p)
        ]
        if remaining:
            out.append({"row_idx": idx, "iso_date": iso, "platforms": remaining})
    return out
