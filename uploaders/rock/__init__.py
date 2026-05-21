"""Rock RMS uploader package.

Split into focused modules:

  - constants:   URLs, channel GUIDs, selector strings
  - fields:      input dataclasses + ItemRef + RockClient Protocol
  - text:        pure helpers (normalize_vista_content, titles, formatters)
  - client:      RockBrowserClient (Playwright lifecycle + page actions)
  - orchestrator: upload_daily_experience (the per-date workflow)
"""
from .constants import _BASE_URL  # re-exported for legacy callers/tests
from .fields import (
    ItemRef,
    ParentFields,
    SpotlightFields,
    VistaFields,
    ReflectionFields,
    EmailFields,
    RockClient,
)
from .text import (
    normalize_vista_content,
    parent_title,
    reflection_title,
    email_title,
    compose_email_message,
    _format_date_for_rock,
    _extract_item_id,
)
from .client import RockBrowserClient
from .orchestrator import upload_daily_experience
from .email import schedule_email

__all__ = [
    "ItemRef",
    "ParentFields",
    "SpotlightFields",
    "VistaFields",
    "ReflectionFields",
    "EmailFields",
    "RockClient",
    "RockBrowserClient",
    "upload_daily_experience",
    "schedule_email",
    "normalize_vista_content",
    "parent_title",
    "reflection_title",
    "email_title",
    "compose_email_message",
]
