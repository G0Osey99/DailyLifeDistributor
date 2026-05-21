"""upload_daily_experience — the per-date Rock workflow.

Owns: idempotency check → image gather → create children → create parent
→ link children → record image use → temp-file cleanup.

Returns the standard uploader result dict so app.py can dispatch uniformly.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime as _dt
from pathlib import Path as _Path
from typing import Optional

from .client import RockBrowserClient
from .fields import ParentFields, ReflectionFields, SpotlightFields, VistaFields
from .text import normalize_vista_content, parent_title, reflection_title


log = logging.getLogger(__name__)


def upload_daily_experience(entry, *, elements=None, progress_callback=None) -> dict:
    """Build one Rock Daily Experience for a ReviewEntry.

    `elements` is the ReviewEntry's UploadElements; per-component flags
    let the user disable individual children. `progress_callback(phase)`
    is called with short string phases for the SSE stream.

    Result dict shape (matches the YouTube/SimpleCast uploaders):

        {"success": bool, "url": str, "error": str, "skipped": bool,
         "scheduled_time": ""}
    """

    def _emit(phase: str) -> None:
        if progress_callback is not None:
            try:
                progress_callback(phase)
            except Exception:  # noqa: BLE001 — progress callback must never break uploads
                log.exception("Rock progress callback raised; continuing")

    elements = elements or entry.elements
    publish_date = _dt.strptime(entry.date, "%Y-%m-%d").date()

    # Validate the inputs we need for each component up front so a
    # mid-flight failure doesn't leave behind a half-built experience.
    rock_spotlight_on = bool(getattr(elements, "rock_spotlight", True))
    rock_vista_on = bool(getattr(elements, "rock_vista", True))
    rock_reflection_on = bool(getattr(elements, "rock_reflection", True))
    rock_image_on = bool(getattr(elements, "rock_image", True))

    missing: list[str] = []
    if rock_spotlight_on:
        if not getattr(entry, "episode_title", "").strip():
            missing.append("episode_title")
        if not getattr(entry, "wistia_ref", "").strip():
            missing.append("wistia_ref")
    if rock_vista_on:
        if not getattr(entry, "passage", "").strip():
            missing.append("passage")
        if not getattr(entry, "scripture", "").strip():
            missing.append("scripture")
    if rock_reflection_on and not getattr(entry, "prayer", "").strip():
        missing.append("prayer")
    if missing:
        return {
            "success": False,
            "skipped": False,
            "url": "",
            "scheduled_time": "",
            "error": f"Missing required fields: {', '.join(missing)}",
        }

    image_temp_path: Optional[str] = None
    image_meta = None  # GatheredImage, used for record_image_use on success
    try:
        if rock_vista_on and rock_image_on:
            _emit("gathering_image")
            from core.image_gatherer import gather_image_for_verse
            image_meta = gather_image_for_verse(
                entry.scripture,
                publish_date,
                topic_hint=getattr(entry, "topic_hint", ""),
            )
            if image_meta is None:
                # Per project policy: fail+warn, don't silently substitute.
                return {
                    "success": False,
                    "skipped": False,
                    "url": "",
                    "scheduled_time": "",
                    "error": (
                        "Image gatherer returned no usable image. "
                        "Check llamafile is running and UNSPLASH_ACCESS_KEY is set."
                    ),
                }
            image_temp_path = image_meta.file_path

        with RockBrowserClient() as rock:
            _emit("checking_existing")
            existing = rock.find_existing_parent_for_date(publish_date)
            if existing is not None:
                log.warning(
                    "Rock: parent for %s already exists (id=%d); skipping",
                    publish_date.isoformat(), existing.id,
                )
                return {
                    "success": True,
                    "skipped": True,
                    "url": existing.edit_url,
                    "scheduled_time": f"{entry.date} 00:00",
                    "error": "",
                }

            spot_ref = vista_ref = refl_ref = None

            if rock_reflection_on:
                _emit("creating_reflection")
                refl_ref = rock.create_reflection(ReflectionFields(
                    title=reflection_title(publish_date),
                    content=entry.prayer,
                ))

            if rock_vista_on:
                _emit("creating_vista")
                vista_ref = rock.create_vista(VistaFields(
                    title=entry.passage,
                    content=normalize_vista_content(entry.scripture, entry.passage),
                    background_image_path=(
                        _Path(image_temp_path) if image_temp_path else None
                    ),
                ))

            if rock_spotlight_on:
                _emit("creating_spotlight")
                spot_ref = rock.create_spotlight(SpotlightFields(
                    title=entry.episode_title,
                    media_reference=entry.wistia_ref,
                ))

            _emit("creating_parent")
            parent_ref = rock.create_parent(ParentFields(
                title=parent_title(publish_date),
                active_date=publish_date,
            ))

            _emit("linking_children")
            if spot_ref is not None:
                rock.link_spotlight_to_parent(parent_ref, spot_ref)
            if vista_ref is not None:
                rock.link_vista_to_parent(parent_ref, vista_ref)
            if refl_ref is not None:
                rock.link_reflection_to_parent(parent_ref, refl_ref)

            # Only record the image use after Rock has accepted everything.
            if image_meta is not None:
                from core import db as _db
                from core.image_gatherer import append_credits_entry
                try:
                    _db.record_image_use(
                        photo_id=image_meta.photo_id,
                        source=image_meta.source,
                        topic=image_meta.topic,
                        used_on_date=entry.date,
                        photographer=image_meta.photographer,
                        photo_url=image_meta.photo_url,
                    )
                except Exception as e:  # noqa: BLE001 — DB hiccup must not fail the upload
                    log.warning("record_image_use failed: %s", e)
                append_credits_entry(
                    used_on_date=entry.date,
                    source=image_meta.source,
                    photographer=image_meta.photographer,
                    photo_url=image_meta.photo_url,
                    topic=image_meta.topic,
                )

            _emit("done")
            # Rock items publish immediately on save (no future schedule), but
            # record the publish date as scheduled_time so the Calendar/History
            # views can place the row on the right day.
            return {
                "success": True,
                "skipped": False,
                "url": parent_ref.edit_url,
                "scheduled_time": f"{entry.date} 00:00",
                "error": "",
            }
    except Exception as e:  # noqa: BLE001 — surface any failure as a row error
        log.exception("Rock Daily Experience build failed for %s", entry.date)
        return {
            "success": False,
            "skipped": False,
            "url": "",
            "scheduled_time": "",
            "error": str(e),
        }
    finally:
        if image_temp_path:
            try:
                os.remove(image_temp_path)
            except OSError:
                pass
