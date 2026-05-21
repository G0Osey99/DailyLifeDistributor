"""schedule_email — the per-date Daily Life *email* workflow.

Owns: input validation → idempotency check → create the email content-
channel item (draft, Sent=No) with the day's description + standing footer,
the horizontal YouTube watch link, and the email-specific thumbnail.

The business rule "emails must be scheduled after YouTube videos within a
flow, or else YouTube links must be provided for the dates" is enforced by
the orchestrator (core.upload_jobs), which resolves the watch URL from the
YouTube Video result of the same run before calling here. This module just
requires a non-empty `youtube_watch_url` and fails loudly without one.

Returns the standard uploader result dict so app.py dispatches uniformly.
"""
from __future__ import annotations

import logging
from datetime import datetime as _dt
from pathlib import Path as _Path
from typing import Optional

from .client import RockBrowserClient
from .fields import EmailFields
from .text import email_title


log = logging.getLogger(__name__)


def schedule_email(
    entry,
    *,
    youtube_watch_url: str = "",
    elements=None,
    progress_callback=None,
) -> dict:
    """Create one Daily Life email item for a ReviewEntry.

    `youtube_watch_url` is the horizontal (non-Shorts) watch link, resolved
    by the caller from this run's YouTube Video upload or a provided link.
    `elements` is the ReviewEntry's UploadElements. `progress_callback(phase)`
    receives short phase strings for the SSE stream.

    Result dict shape (matches the other uploaders):

        {"success": bool, "url": str, "error": str, "skipped": bool,
         "scheduled_time": ""}
    """

    def _emit(phase: str) -> None:
        if progress_callback is not None:
            try:
                progress_callback(phase)
            except Exception:  # noqa: BLE001 — progress cb must never break the upload
                log.exception("Rock email progress callback raised; continuing")

    elements = elements or entry.elements
    publish_date = _dt.strptime(entry.date, "%Y-%m-%d").date()

    # The YouTube link is mandatory: the email leads with the video, so a
    # missing link means the run is mis-ordered (email before YouTube) or no
    # link was provided for the date. Fail loudly rather than ship a linkless
    # email.
    watch_url = (youtube_watch_url or getattr(entry, "youtube_watch_url", "") or "").strip()
    if not watch_url:
        return {
            "success": False,
            "skipped": False,
            "url": "",
            "scheduled_time": "",
            "error": (
                "No YouTube link available for this date. Either include the "
                "YouTube Video upload in this run (so the email can follow it) "
                "or provide a YouTube link for the date."
            ),
        }

    want_thumb = bool(getattr(elements, "rock_email_thumbnail", True))
    thumb_path: Optional[_Path] = None
    if want_thumb:
        raw = getattr(entry, "email_thumbnail_path", "") or ""
        if raw:
            thumb_path = _Path(raw)
            if not thumb_path.is_file():
                return {
                    "success": False,
                    "skipped": False,
                    "url": "",
                    "scheduled_time": "",
                    "error": f"Email thumbnail not found on disk: {raw}",
                }

    fields = EmailFields(
        title=email_title(publish_date),
        start_date=publish_date,
        description=getattr(entry, "description", "") or "",
        youtube_watch_url=watch_url,
        thumbnail_path=thumb_path,
    )

    try:
        with RockBrowserClient() as rock:
            _emit("checking_existing")
            existing = rock.find_existing_email_for_date(fields)
            if existing is not None:
                log.warning(
                    "Rock email for %s already exists (id=%d); skipping",
                    entry.date, existing.id,
                )
                return {
                    "success": True,
                    "skipped": True,
                    "url": existing.edit_url,
                    "scheduled_time": f"{entry.date} 00:00",
                    "error": "",
                }

            _emit("creating_email")
            ref = rock.create_email_item(fields)
            _emit("done")
            return {
                "success": True,
                "skipped": False,
                "url": ref.edit_url,
                "scheduled_time": f"{entry.date} 00:00",
                "error": "",
            }
    except Exception as e:  # noqa: BLE001 — surface any failure as a row error
        log.exception("Rock email build failed for %s", entry.date)
        return {
            "success": False,
            "skipped": False,
            "url": "",
            "scheduled_time": "",
            "error": str(e),
        }
