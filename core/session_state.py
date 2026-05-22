"""Holds in-memory upload session state (selected dates, edits, results)."""

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo

from core.config import load_config as _load_config


_WISTIA_REF_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def infer_wistia_ref(file_path: Optional[str]) -> str:
    """Best-effort Wistia media-reference label from a shorts filename.

    The Wistia uploads use labels of the form ``app YYMMDD`` (e.g.
    ``app 260510``). We extract the first 6-digit run from the filename
    stem and prepend ``app ``. Returns an empty string if no run is
    found — the caller is expected to surface that in the UI so the
    user can supply one manually.
    """
    if not file_path:
        return ""
    stem = os.path.splitext(os.path.basename(file_path))[0]
    m = _WISTIA_REF_RE.search(stem)
    return f"app {m.group(1)}" if m else ""


@dataclass
class UploadElements:
    """Per-platform toggle flags controlling which upload elements are active."""

    # YouTube Video
    yt_video_enabled: bool = True
    yt_video_thumbnail: bool = True
    yt_video_title: bool = True
    yt_video_description: bool = True
    yt_video_tags: bool = True
    yt_video_schedule: bool = True

    # YouTube Shorts
    yt_shorts_enabled: bool = True
    yt_shorts_thumbnail: bool = True
    yt_shorts_title: bool = True
    yt_shorts_description: bool = True
    yt_shorts_tags: bool = True
    yt_shorts_schedule: bool = True

    # SimpleCast
    sc_enabled: bool = True
    sc_thumbnail: bool = True
    sc_description: bool = True
    sc_schedule: bool = True

    # Rock (Daily Experience: parent + Spotlight + Vista + Reflection)
    rock_enabled: bool = True
    rock_spotlight: bool = True   # create + link the Spotlight (video) child
    rock_vista: bool = True       # create + link the Vista (verse) child
    rock_reflection: bool = True  # create + link the Reflection (prayer) child
    rock_image: bool = True       # gather + upload Vista background image

    # Rock Daily Life email (separate channel: queued email/SMS broadcast)
    rock_email_enabled: bool = True
    rock_email_thumbnail: bool = True  # upload the email-specific thumbnail

    # Vista Social (schedules a single post to Instagram + Facebook)
    vs_enabled: bool = True
    vs_description: bool = True
    vs_schedule: bool = True

    def to_dict(self) -> dict:
        # Reflect over the dataclass fields rather than spelling each one out.
        # Adding a new toggle now requires only the dataclass field; the
        # serializer + deserializer + config-default loader all pick it up.
        return {fld: bool(getattr(self, fld)) for fld in self.__dataclass_fields__}

    @classmethod
    def from_config(cls, config: dict) -> "UploadElements":
        """Build UploadElements using defaults from config.yaml."""
        defaults = config.get("defaults", {}).get("elements", {})
        kwargs = {}
        for fld in cls.__dataclass_fields__:
            if fld in defaults:
                kwargs[fld] = bool(defaults[fld])
        return cls(**kwargs)


@dataclass
class ReviewEntry:
    """All editable fields for a single date's upload session."""

    date: str
    display_date: str
    youtube_video_path: Optional[str] = None
    youtube_shorts_path: Optional[str] = None
    podcast_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    # Email-specific thumbnail (the YouTube-play-button-overlay variant),
    # scanned from its own media directory; used only by the Rock email item.
    email_thumbnail_path: Optional[str] = None
    youtube_title: str = ""
    youtube_shorts_title: str = ""
    podcast_title: str = ""
    description: str = ""
    tags: list = field(default_factory=list)
    # Rock RMS Daily Experience fields (read from Excel)
    passage: str = ""          # Vista title, e.g. "Acts 28:30-31"
    scripture: str = ""        # Vista content, e.g. "<verse> - <ref>"
    episode_title: str = ""    # Spotlight title (Excel "Episode Title")
    prayer: str = ""           # Reflection content (Excel "Prayer")
    topic_hint: str = ""       # Excel "Topic" — fed to image gatherer
    transcript: str = ""       # Excel "Transcript" — fed to LLM title suggestions
    wistia_ref: str = ""       # Spotlight Media dropdown label (e.g. "app 260510")
    # Horizontal (non-Shorts) YouTube watch link for the Daily Life email.
    # Normally captured from this run's YouTube Video upload; this field is
    # the per-date fallback when YouTube isn't part of the run.
    youtube_watch_url: str = ""
    youtube_schedule_dt: Optional[datetime] = None
    shorts_schedule_dt: Optional[datetime] = None
    podcast_schedule_dt: Optional[datetime] = None
    vista_schedule_dt: Optional[datetime] = None
    # Vista Social caption — falls back to `description` when blank.
    # Kept separate so an Instagram-tuned Excel column can override the
    # YouTube description when present.
    vista_caption: str = ""
    llm_title_suggestions: list = field(default_factory=list)
    platforms_enabled: dict = field(default_factory=dict)
    elements: UploadElements = field(default_factory=UploadElements)

    # Field names that hold datetimes; serialized as ISO strings.
    # Computed at class-load via the dataclass annotations so that adding a
    # new datetime field requires only the annotation, not a hand edit here.
    _DATETIME_FIELDS = (
        "youtube_schedule_dt",
        "shorts_schedule_dt",
        "podcast_schedule_dt",
        "vista_schedule_dt",
    )

    def to_dict(self) -> dict:
        """Serialize to a plain dict for templates and JSON responses.

        Iterates `__dataclass_fields__` so a new field on ReviewEntry
        flows through automatically — no parallel edit. Datetimes get
        ISO strings; the nested UploadElements gets its own to_dict.
        """
        out: dict = {}
        for fld in self.__dataclass_fields__:
            value = getattr(self, fld)
            if fld == "elements":
                out[fld] = value.to_dict()
            elif fld in self._DATETIME_FIELDS:
                out[fld] = value.isoformat() if value else None
            else:
                out[fld] = value
        return out


class SessionState:
    """In-memory session state for a single upload workflow."""

    # Minimum gap between successive save() writes for the same session.
    # /review/update fires per keystroke; without this we'd serialize and
    # rewrite the entire entry blob 10+ times per second.
    _SAVE_DEBOUNCE_SEC = 1.5

    def __init__(self):
        self.session_id: str = str(uuid.uuid4())
        self.selected_dates: list[str] = []
        self.entries: dict[str, ReviewEntry] = {}
        self.upload_results: dict[str, dict] = {}
        self._config = _load_config()
        self.global_times: dict[str, str] = {}
        # Coarse mutex for cross-thread reads/writes of the singleton. The
        # upload worker pool, the SSE consumer, and the user's review tab
        # all touch `entries` / `upload_results` concurrently. The lock is
        # re-entrant so a method that already holds it can call helpers
        # that also acquire (e.g. update_entry → save → _to_state_dict).
        self._lock = threading.RLock()
        # Debounced save: track when we last persisted and whether a save
        # is pending in the background timer.
        self._last_save_at: float = 0.0
        self._pending_save_timer: Optional[threading.Timer] = None
        # M8/M22: surfaced to the UI so the user sees a banner when
        # background metadata loads or session persistence quietly failed.
        self.excel_last_error: str = ""
        self.persistence_error: str = ""

    def reload_config(self) -> None:
        """Re-read config.yaml from disk so subsequent calls see the latest
        scheduling/timezone/platform defaults. Call this after the Settings
        page writes config.yaml — otherwise the singleton's _config is stale
        for the lifetime of the process."""
        self._config = _load_config()

    def _default_schedule(self, iso_date: str, time_str: str) -> datetime:
        """Build a timezone-aware datetime from a date and time string."""
        tz_name = self._config.get("scheduling", {}).get("timezone", "America/New_York")
        tz = ZoneInfo(tz_name)
        # M23: a malformed `global_times` value (e.g. "ten" or "10:00:00")
        # used to 500 the index POST. Fall back to 09:00 with a warning so
        # the workflow can proceed.
        try:
            parts = time_str.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError(f"hour/minute out of range in {time_str!r}")
        except (ValueError, TypeError, IndexError) as e:
            _log.warning("Invalid time_str %r for %s — defaulting to 09:00 (%s)", time_str, iso_date, e)
            hour, minute = 9, 0
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return dt.replace(hour=hour, minute=minute, second=0, tzinfo=tz)

    def _default_elements(self) -> UploadElements:
        """Return UploadElements initialized from config defaults."""
        return UploadElements.from_config(self._config)

    def _resolve_global_platforms(self, override: Optional[dict] = None) -> dict:
        if override is not None:
            return dict(override)
        platforms_cfg = self._config.get("platforms", {})
        return {
            "youtube_video": platforms_cfg.get("youtube_video", True),
            "youtube_shorts": platforms_cfg.get("youtube_shorts", True),
            "simplecast": platforms_cfg.get("simplecast", True),
            "rock": platforms_cfg.get("rock", True),
            "rock_email": platforms_cfg.get("rock_email", False),
            "vista_social": platforms_cfg.get("vista_social", True),
        }

    def _scheduled_times(self) -> tuple[str, str, str, str]:
        sched = self._config.get("scheduling", {})
        return (
            self.global_times.get("youtube_video") or sched.get("youtube_video", "10:00"),
            self.global_times.get("youtube_shorts") or sched.get("youtube_shorts", "12:00"),
            self.global_times.get("simplecast") or sched.get("simplecast", "06:00"),
            self.global_times.get("vista_social") or sched.get("vista_social", "12:00"),
        )

    def build_entry(
        self,
        iso_date: str,
        media,
        meta: Optional[dict] = None,
        global_platforms: Optional[dict] = None,
    ) -> "ReviewEntry":
        """Construct a ReviewEntry from a media match + Excel metadata row.

        Used by the media pipeline's per-batch entry build (blueprints/media.py).
        `media` is a MediaDateEntry-like object (carrying the batch's temp file
        paths) or None; `meta` is a parsed spreadsheet row.
        """
        meta = meta or {}
        yt_video_time, yt_shorts_time, sc_time, vs_time = self._scheduled_times()
        platforms_enabled = self._resolve_global_platforms(global_platforms)

        display_date = datetime.strptime(iso_date, "%Y-%m-%d").strftime("%B %d, %Y")

        youtube_title = meta.get("youtube_title", "")
        podcast_title = meta.get("podcast_title", "") or youtube_title
        shorts_title = meta.get("shorts_title", "") or youtube_title

        shorts_path = media.youtube_shorts_path if media else None

        return ReviewEntry(
            date=iso_date,
            display_date=display_date,
            youtube_video_path=media.youtube_video_path if media else None,
            youtube_shorts_path=shorts_path,
            podcast_path=media.podcast_path if media else None,
            thumbnail_path=media.thumbnail_path if media else None,
            email_thumbnail_path=getattr(media, "email_thumbnail_path", None) if media else None,
            youtube_title=youtube_title,
            youtube_shorts_title=shorts_title,
            podcast_title=podcast_title,
            description=meta.get("description", ""),
            tags=meta.get("tags", []),
            passage=meta.get("passage", ""),
            scripture=meta.get("scripture", ""),
            episode_title=meta.get("episode_title", "") or youtube_title,
            prayer=meta.get("prayer", ""),
            topic_hint=meta.get("topic", ""),
            transcript=meta.get("transcript", ""),
            wistia_ref=infer_wistia_ref(shorts_path),
            youtube_schedule_dt=self._default_schedule(iso_date, yt_video_time),
            shorts_schedule_dt=self._default_schedule(iso_date, yt_shorts_time),
            podcast_schedule_dt=self._default_schedule(iso_date, sc_time),
            vista_schedule_dt=self._default_schedule(iso_date, vs_time),
            vista_caption=meta.get("vista_caption", "") or meta.get("description", ""),
            llm_title_suggestions=[],
            platforms_enabled=platforms_enabled,
            elements=self._default_elements(),
        )

    def update_entry(self, date: str, field_name: str, value):
        """Update a single field on a ReviewEntry.

        Supports dot-notation for elements fields, e.g.
        "elements.yt_video_thumbnail" maps to entry.elements.yt_video_thumbnail.
        """
        with self._lock:
            return self._update_entry_locked(date, field_name, value)

    def _update_entry_locked(self, date: str, field_name: str, value):
        if date not in self.entries:
            return False

        entry = self.entries[date]

        def _persist_and_return_true() -> bool:
            try:
                self.save()
            except Exception as e:
                # M22: persistence failures are non-fatal for the in-memory
                # workflow but must not be invisible.
                _log.warning("session.save (update_entry) failed: %s", e)
                self.persistence_error = str(e)
            return True

        # Handle dot-notation for elements fields
        if field_name.startswith("elements."):
            attr = field_name[len("elements."):]
            if hasattr(entry.elements, attr):
                setattr(entry.elements, attr, bool(value))
                return _persist_and_return_true()
            return False

        if field_name == "tags" and isinstance(value, str):
            value = [t.strip() for t in value.split(",") if t.strip()]

        if field_name == "platforms_enabled" and isinstance(value, dict):
            entry.platforms_enabled.update(value)
            return _persist_and_return_true()

        if field_name in (
            "youtube_schedule_dt",
            "shorts_schedule_dt",
            "podcast_schedule_dt",
            "vista_schedule_dt",
        ):
            if isinstance(value, str) and value:
                try:
                    tz_name = self._config.get("scheduling", {}).get(
                        "timezone", "America/New_York"
                    )
                    tz = ZoneInfo(tz_name)
                    dt = datetime.fromisoformat(value)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=tz)
                    setattr(entry, field_name, dt)
                    return _persist_and_return_true()
                except (ValueError, AttributeError):
                    return False
            elif value is None:
                setattr(entry, field_name, None)
                return _persist_and_return_true()

        if hasattr(entry, field_name):
            setattr(entry, field_name, value)
            return _persist_and_return_true()

        return False

    def record_result(self, iso_date: str, platform: str, result: dict) -> None:
        """Thread-safe write into upload_results from the upload worker pool.

        The executor's `as_completed` thread, the SSE consumer, and review
        PATCHes all touch upload_results concurrently. Funnel writes through
        this helper so callers cannot forget to take the singleton's lock.
        """
        with self._lock:
            self.upload_results.setdefault(iso_date, {})[platform] = result

    def pop_result(self, iso_date: str) -> None:
        """Thread-safe drop of upload_results for one date (rescan/reinterpret)."""
        with self._lock:
            self.upload_results.pop(iso_date, None)

    def update_all_entries(self, field_name: str, value) -> int:
        """Update a field across ALL entries. Returns count of updated entries."""
        # Take the lock once for the whole loop so a concurrent reader
        # never sees a partially-applied bulk edit.
        with self._lock:
            count = 0
            for date in self.selected_dates:
                if self._update_entry_locked(date, field_name, value):
                    count += 1
            return count

    def get_summary(self) -> list[dict]:
        """Return a list of summary dicts for the confirm view."""
        summaries = []
        for iso_date in self.selected_dates:
            entry = self.entries.get(iso_date)
            if not entry:
                continue

            elems = entry.elements

            # YouTube Video row
            if entry.platforms_enabled.get("youtube_video") and entry.youtube_video_path:
                skipped = not elems.yt_video_enabled
                elements_info = self._elements_summary(
                    enabled=elems.yt_video_enabled,
                    thumbnail=elems.yt_video_thumbnail,
                    title=elems.yt_video_title,
                    description=elems.yt_video_description,
                    tags=elems.yt_video_tags,
                    schedule=elems.yt_video_schedule,
                )
                summaries.append(
                    {
                        "date": entry.display_date,
                        "iso_date": entry.date,
                        "platform": "YouTube Video",
                        "title": entry.youtube_title,
                        "scheduled_time": (
                            entry.youtube_schedule_dt.strftime("%Y-%m-%d %H:%M %Z")
                            if entry.youtube_schedule_dt
                            else "Immediate"
                        ),
                        "file": entry.youtube_video_path,
                        "thumbnail": entry.thumbnail_path or "—",
                        "elements": elements_info,
                        "skipped": skipped,
                    }
                )

            # YouTube Shorts row
            if entry.platforms_enabled.get("youtube_shorts") and entry.youtube_shorts_path:
                skipped = not elems.yt_shorts_enabled
                elements_info = self._elements_summary(
                    enabled=elems.yt_shorts_enabled,
                    thumbnail=elems.yt_shorts_thumbnail,
                    title=elems.yt_shorts_title,
                    description=elems.yt_shorts_description,
                    tags=elems.yt_shorts_tags,
                    schedule=elems.yt_shorts_schedule,
                )
                summaries.append(
                    {
                        "date": entry.display_date,
                        "iso_date": entry.date,
                        "platform": "YouTube Shorts",
                        "title": entry.youtube_shorts_title,
                        "scheduled_time": (
                            entry.shorts_schedule_dt.strftime("%Y-%m-%d %H:%M %Z")
                            if entry.shorts_schedule_dt
                            else "Immediate"
                        ),
                        "file": entry.youtube_shorts_path,
                        "thumbnail": entry.thumbnail_path or "—",
                        "elements": elements_info,
                        "skipped": skipped,
                    }
                )

            # Rock row — fires once per date and produces parent + 3 children.
            # We don't gate on a media file because Rock only needs metadata
            # plus a Wistia reference; the verse image is gathered at runtime.
            if entry.platforms_enabled.get("rock"):
                skipped = not entry.elements.rock_enabled
                rock_elements_info = self._elements_summary_rock(
                    enabled=entry.elements.rock_enabled,
                    spotlight=entry.elements.rock_spotlight,
                    vista=entry.elements.rock_vista,
                    reflection=entry.elements.rock_reflection,
                    image=entry.elements.rock_image,
                )
                from datetime import datetime as _dt
                _d = _dt.strptime(entry.date, "%Y-%m-%d")
                rock_title = f"Daily Life {_d.strftime('%B')} {_d.day}"
                summaries.append(
                    {
                        "date": entry.display_date,
                        "iso_date": entry.date,
                        "platform": "Rock",
                        "title": rock_title,
                        "scheduled_time": "—",
                        "file": entry.youtube_shorts_path or "—",
                        "thumbnail": "—",
                        "elements": rock_elements_info,
                        "skipped": skipped,
                    }
                )

            # Rock Email row — queues one Daily Life email item per date. Must
            # run after the YouTube Video upload in the same flow so it can use
            # that watch link (or fall back to entry.youtube_watch_url).
            if entry.platforms_enabled.get("rock_email"):
                skipped = not entry.elements.rock_email_enabled
                from datetime import datetime as _dt
                _d = _dt.strptime(entry.date, "%Y-%m-%d")
                email_row_title = f"Daily Life {_d.strftime('%B')} {_d.day}, {_d.year}"
                thumb_on = entry.elements.rock_email_thumbnail
                email_elements_info = (
                    "Skipped" if skipped
                    else ("Email ✓ | Thumb ✓" if thumb_on else "Email ✓ | Thumb ✗")
                )
                summaries.append(
                    {
                        "date": entry.display_date,
                        "iso_date": entry.date,
                        "platform": "Rock Email",
                        "title": email_row_title,
                        "scheduled_time": f"{entry.date} (send date)",
                        "file": entry.email_thumbnail_path or "—",
                        "thumbnail": entry.email_thumbnail_path or "—",
                        "elements": email_elements_info,
                        "skipped": skipped,
                    }
                )

            # Vista Social row — schedules a single post to Instagram + Facebook
            # using the YouTube Shorts video as the media.
            if entry.platforms_enabled.get("vista_social") and entry.youtube_shorts_path:
                skipped = not elems.vs_enabled
                vs_elements_info = self._elements_summary_vs(
                    enabled=elems.vs_enabled,
                    description=elems.vs_description,
                    schedule=elems.vs_schedule,
                )
                summaries.append(
                    {
                        "date": entry.display_date,
                        "iso_date": entry.date,
                        "platform": "Vista Social",
                        "title": (entry.vista_caption or entry.youtube_shorts_title
                                  or entry.youtube_title)[:80],
                        "scheduled_time": (
                            entry.vista_schedule_dt.strftime("%Y-%m-%d %H:%M %Z")
                            if entry.vista_schedule_dt
                            else "Immediate"
                        ),
                        "file": entry.youtube_shorts_path,
                        "thumbnail": "—",
                        "elements": vs_elements_info,
                        "skipped": skipped,
                    }
                )

            # SimpleCast row
            if entry.platforms_enabled.get("simplecast") and entry.podcast_path:
                skipped = not elems.sc_enabled
                elements_info = self._elements_summary_sc(
                    enabled=elems.sc_enabled,
                    thumbnail=elems.sc_thumbnail,
                    description=elems.sc_description,
                    schedule=elems.sc_schedule,
                )
                summaries.append(
                    {
                        "date": entry.display_date,
                        "iso_date": entry.date,
                        "platform": "SimpleCast",
                        "title": entry.podcast_title,
                        "scheduled_time": (
                            entry.podcast_schedule_dt.strftime("%Y-%m-%d %H:%M %Z")
                            if entry.podcast_schedule_dt
                            else "Immediate"
                        ),
                        "file": entry.podcast_path,
                        "thumbnail": "—",
                        "elements": elements_info,
                        "skipped": skipped,
                    }
                )

        return summaries

    @staticmethod
    def _elements_summary(enabled, thumbnail, title, description, tags, schedule) -> str:
        if not enabled:
            return "Skipped"
        parts = []
        parts.append("Video ✓" if enabled else "Video ✗")
        parts.append("Thumb ✓" if thumbnail else "Thumb ✗")
        parts.append("Title ✓" if title else "Title ✗")
        parts.append("Desc ✓" if description else "Desc ✗")
        parts.append("Tags ✓" if tags else "Tags ✗")
        parts.append("Sched ✓" if schedule else "Sched ✗")
        return " | ".join(parts)

    @staticmethod
    def _elements_summary_rock(enabled, spotlight, vista, reflection, image) -> str:
        if not enabled:
            return "Skipped"
        parts = [
            "Spot ✓" if spotlight else "Spot ✗",
            "Vista ✓" if vista else "Vista ✗",
            "Refl ✓" if reflection else "Refl ✗",
            "Img ✓" if image else "Img ✗",
        ]
        return " | ".join(parts)

    @staticmethod
    def _elements_summary_vs(enabled, description, schedule) -> str:
        if not enabled:
            return "Skipped"
        parts = [
            "Post ✓",
            "IG+FB ✓",
            "Desc ✓" if description else "Desc ✗",
            "Sched ✓" if schedule else "Sched ✗",
        ]
        return " | ".join(parts)

    @staticmethod
    def _elements_summary_sc(enabled, thumbnail, description, schedule) -> str:
        if not enabled:
            return "Skipped"
        parts = []
        parts.append("Episode ✓" if enabled else "Episode ✗")
        parts.append("Art ✓" if thumbnail else "Art ✗")
        parts.append("Desc ✓" if description else "Desc ✗")
        parts.append("Sched ✓" if schedule else "Sched ✗")
        return " | ".join(parts)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _to_state_dict(self) -> dict:
        """Serialize this session to a plain dict suitable for JSON storage."""
        return {
            "selected_dates": self.selected_dates,
            "entries": {date: entry.to_dict() for date, entry in self.entries.items()},
            "upload_results": self.upload_results,
            "global_times": self.global_times,
        }

    @staticmethod
    def _entry_from_dict(data: dict) -> "ReviewEntry":
        """Reconstruct a ReviewEntry (including nested UploadElements) from a dict.

        Mirrors ReviewEntry.to_dict: iterate the dataclass fields, parse
        datetimes back from ISO strings, and rebuild the nested
        UploadElements. Adding a new field requires only the dataclass
        annotation — no parallel edit here.
        """

        def _parse_dt(val):
            if not val:
                return None
            try:
                return datetime.fromisoformat(val)
            except (ValueError, TypeError):
                return None

        elements_data = data.get("elements") or {}
        elements_kwargs = {
            fld: bool(elements_data[fld])
            for fld in UploadElements.__dataclass_fields__
            if fld in elements_data
        }
        elements = UploadElements(**elements_kwargs)

        kwargs: dict = {}
        for fld, descriptor in ReviewEntry.__dataclass_fields__.items():
            if fld == "elements":
                kwargs[fld] = elements
                continue
            if fld in ReviewEntry._DATETIME_FIELDS:
                kwargs[fld] = _parse_dt(data.get(fld))
                continue
            if fld in data:
                kwargs[fld] = data[fld]
            # else: omit so the dataclass default applies. This keeps
            # forward-compatibility when state.db rows pre-date a new field.
        return ReviewEntry(**kwargs)

    def save(self) -> None:
        """Persist the current session to SQLite, debounced.

        /review/update fires per keystroke and previously hit SQLite each
        time, serializing ~30 entries on every character. We coalesce
        saves: writes within _SAVE_DEBOUNCE_SEC of the last persistence
        are queued on a single timer that flushes once the burst settles.
        """
        with self._lock:
            self._last_save_at  # touch to keep mypy/pyright happy
            elapsed = time.monotonic() - self._last_save_at
            if elapsed >= self._SAVE_DEBOUNCE_SEC:
                self._save_now_locked()
                return
            # Burst case: schedule a deferred flush if one isn't already
            # pending. The timer runs flush_pending_save() which acquires
            # the lock itself.
            if self._pending_save_timer is None:
                delay = max(self._SAVE_DEBOUNCE_SEC - elapsed, 0.05)
                t = threading.Timer(delay, self.flush_pending_save)
                t.daemon = True
                self._pending_save_timer = t
                t.start()

    def flush_pending_save(self) -> None:
        """Write any pending state to SQLite immediately.

        Safe to call from any thread. Used by the debounce timer and by
        the upload pipeline (which wants the latest state on disk before
        crossing into upload).
        """
        with self._lock:
            self._pending_save_timer = None
            self._save_now_locked()

    def _save_now_locked(self) -> None:
        """Inner save that assumes the caller holds self._lock."""
        from core import db  # local import to avoid circular issues at module load

        label = ", ".join(self.selected_dates) if self.selected_dates else "(no dates)"
        state_json = json.dumps(self._to_state_dict())
        existing = db.load_session(self.session_id)
        status = "completed" if existing and existing.get("status") == "completed" else "in_progress"
        db.save_session(self.session_id, label, state_json, status=status)
        self._last_save_at = time.monotonic()

    @classmethod
    def load(cls, session_id: str) -> Optional["SessionState"]:
        """Load a SessionState from the database. Returns None if not found."""
        from core import db

        row = db.load_session(session_id)
        if not row:
            return None
        return cls._from_db_row(row)

    @classmethod
    def resume_latest(cls) -> Optional["SessionState"]:
        """Load the most recent in-progress session, or None if none exists."""
        from core import db

        row = db.get_latest_in_progress()
        if not row:
            return None
        return cls._from_db_row(row)

    @classmethod
    def _from_db_row(cls, row: dict) -> "SessionState":
        """Reconstruct a SessionState from a database row dict."""
        obj = cls.__new__(cls)
        # Reinitialise fields that __init__ would normally set. Without
        # this the lock and debounce attrs would be missing, and the next
        # update_entry call would AttributeError under `with self._lock`.
        obj._config = _load_config()
        obj._lock = threading.RLock()
        obj._last_save_at = 0.0
        obj._pending_save_timer = None
        obj.excel_last_error = ""
        obj.persistence_error = ""

        try:
            state = json.loads(row.get("state_json") or "{}")
        except (ValueError, TypeError):
            state = {}

        obj.session_id = row.get("id", str(uuid.uuid4()))
        obj.selected_dates = state.get("selected_dates", [])
        obj.global_times = state.get("global_times", {})
        obj.upload_results = state.get("upload_results", {})

        raw_entries = state.get("entries", {})
        # M21: a single bad stored entry used to crash the whole resume.
        # Skip per-entry on failure so the rest of the session can be
        # restored, and surface a banner via persistence_error.
        obj.entries = {}
        skipped: list[str] = []
        for date, data in raw_entries.items():
            try:
                obj.entries[date] = cls._entry_from_dict(data)
            except Exception as e:
                _log.warning("Could not restore entry for %s: %s", date, e)
                skipped.append(date)
        if skipped:
            obj.persistence_error = (
                f"Could not restore entries for {len(skipped)} date(s): "
                f"{', '.join(skipped[:5])}{'...' if len(skipped) > 5 else ''}"
            )

        return obj

    # ------------------------------------------------------------------

    def clear(self):
        """Reset session state."""
        with self._lock:
            self.selected_dates.clear()
            self.entries.clear()
            self.upload_results.clear()
            self.global_times.clear()

    def replace_with(self, other: "SessionState") -> None:
        """Copy another SessionState's fields onto this one in place.

        Used by `/resume-session` so that the module-level singleton stays
        the same object — no need to rebind names in caller modules.
        """
        with self._lock:
            self.session_id = other.session_id
            self.selected_dates = other.selected_dates
            self.entries = other.entries
            self.upload_results = other.upload_results
            self.global_times = other.global_times
            self._config = other._config


# Module-level singleton instance
session = SessionState()
