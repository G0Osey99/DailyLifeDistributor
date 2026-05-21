"""Input dataclasses + ItemRef + RockClient Protocol.

Every Rock interaction takes one of these dataclasses as its source of
truth. Keeping them isolated from the Playwright client means tests can
construct fields without importing Playwright.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Protocol

from .constants import _BASE_URL


@dataclass(frozen=True)
class ItemRef:
    """Stable reference to a Rock content channel item."""

    id: int

    @property
    def edit_url(self) -> str:
        return f"{_BASE_URL}/ContentChannelItem/{self.id}"


@dataclass
class ParentFields:
    title: str               # e.g. "Daily Life April 12"
    active_date: date        # publish date
    enable_prayer: bool = False
    prayer_count: int = 3


@dataclass
class SpotlightFields:
    title: str               # Excel "Episode Title"
    tagline: str = "Watch"
    video_orientation: str = "Vertical"
    media_account: str = "Wistia"
    media_folder: str = "Daily Life App"
    media_reference: str = ""  # e.g. "app 260510" — must match a Wistia option


@dataclass
class VistaFields:
    title: str               # Excel "Passage" (e.g. "Acts 28:30–31")
    content: str             # Excel "Scripture"
    tagline: str = "Verse"
    has_likes_enabled: bool = True
    background_image_path: Optional[Path] = None  # set Background Image only;
                                                  # Rock auto-derives Share Image


@dataclass
class ReflectionFields:
    title: str               # e.g. "Apr 12" — strftime("%b %-d") / "%#d" on Windows
    content: str             # Excel "Prayer"
    public_title: str = "Pause & Pray"
    prompt_title: str = "Personal Reflection"
    prompt: str = "What is your takeaway for today?"


@dataclass
class EmailFields:
    """One Daily Life *email* content-channel item.

    The body text is composed by the orchestrator (description + the
    channel's standing footer); `youtube_watch_url` is the horizontal
    (non-Shorts) watch link captured from the YouTube Video upload in the
    same run, or supplied per date. `thumbnail_path` is the email-specific
    thumbnail (the variant with the YouTube play-button overlay) from its
    own media directory.
    """

    title: str               # e.g. "Daily Life May 31, 2026"
    start_date: date         # the send/publish date (Rock "Start")
    description: str = ""    # day's devotional line, prepended above footer
    youtube_watch_url: str = ""   # horizontal video link -> YouTube Link field
    thumbnail_path: Optional[Path] = None
    mirror_to_sms: bool = True    # SMS Message mirrors the Email Message body


class RockClient(Protocol):
    """Surface the orchestrator talks to. Browser & API impls satisfy this."""

    def find_existing_parent_for_date(self, publish_date: date) -> Optional[ItemRef]: ...
    def create_parent(self, fields: ParentFields) -> ItemRef: ...
    def create_spotlight(self, fields: SpotlightFields) -> ItemRef: ...
    def create_vista(self, fields: VistaFields) -> ItemRef: ...
    def create_reflection(self, fields: ReflectionFields) -> ItemRef: ...
    def link_spotlight_to_parent(self, parent: ItemRef, spotlight: ItemRef) -> None: ...
    def link_vista_to_parent(self, parent: ItemRef, vista: ItemRef) -> None: ...
    def link_reflection_to_parent(self, parent: ItemRef, reflection: ItemRef) -> None: ...
