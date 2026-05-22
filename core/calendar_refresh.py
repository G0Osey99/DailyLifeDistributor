"""Orchestrate parallel refresh across YouTube / SimpleCast / Rock sources.

Each source module exposes:
    NAME: str           # source label (informational; per-item platform may differ)
    PLATFORMS: list[str]  # DB platform values this source can emit
    fetch(start, end)   # returns list[ExternalItem]; may raise SessionExpiredError

This module owns the threading lock, per-source error isolation, and DB persistence.
Sources stay pure data-producers.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from core import db
from core.playwright_session import SessionExpiredError  # re-exported for sources

log = logging.getLogger(__name__)

__all__ = ["SessionExpiredError", "ExternalItem", "run_refresh", "get_configured_sources"]


@dataclass
class ExternalItem:
    platform: str
    external_id: str
    iso_date: str
    scheduled_time: str
    title: str
    url: str
    status: str
    raw_json: str = "{}"

    def to_dict(self) -> dict:
        return asdict(self)


_LOCK = threading.Lock()


def _fetch_one(source, start: date, end: date, timeout_sec: int) -> dict:
    """Run one source under a thread-pool timeout, return a result dict."""
    name = source.NAME
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(source.fetch, start, end)
            items = fut.result(timeout=timeout_sec)
    except FuturesTimeoutError:
        # IMPORTANT: ThreadPoolExecutor cannot kill a Python thread, so the
        # underlying source.fetch() is still running — almost certainly a
        # Playwright/Chrome subprocess hung on a selector wait. The thread
        # (and its Chrome process) will leak until the OS reaps them.
        # Log loudly so repeated leaks are visible in the server log; the
        # next refresh that hits the same hung dependency will compound the
        # leak. Long term this needs a process-pool isolation for browser
        # sources, but at minimum a loud warning surfaces the issue.
        log.warning(
            "calendar refresh: source %r exceeded %ds timeout — worker thread "
            "may still be running (Playwright/Chrome cannot be force-killed "
            "from here). Repeated occurrences indicate a leaked browser process.",
            name, timeout_sec,
        )
        return {"name": name, "ok": False, "error": "timeout", "items": None}
    except SessionExpiredError as e:
        # Flagged separately so the UI can offer a "Re-login" button rather
        # than just a generic error string.
        return {
            "name": name,
            "ok": False,
            "error": f"session expired: {e}",
            "session_expired": True,
            "items": None,
        }
    except Exception as e:  # noqa: BLE001 - we want to catch and surface anything
        return {"name": name, "ok": False, "error": str(e) or e.__class__.__name__, "items": None}
    return {"name": name, "ok": True, "error": None, "items": items}


def run_refresh(
    sources: list,
    window_days_back: int = 30,
    window_days_forward: int = 180,
    source_timeout_sec: int = 180,
) -> dict:
    """Refresh all sources in parallel.

    Returns {"busy": True} if another refresh is in progress.
    """
    if not _LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        today = date.today()
        start = today - timedelta(days=window_days_back)
        end = today + timedelta(days=window_days_forward)
        iso_start, iso_end = start.isoformat(), end.isoformat()

        per_source_results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max(len(sources), 1)) as pool:
            futures = {
                pool.submit(_fetch_one, s, start, end, source_timeout_sec): s
                for s in sources
            }
            for fut in as_completed(futures):
                r = fut.result()
                source_obj = futures[fut]
                if r["ok"]:
                    items = r["items"] or []
                    db.upsert_external_items([it.to_dict() for it in items])
                    # Group seen ids by emitted platform; mark stale per-platform.
                    by_platform: dict[str, set[str]] = {}
                    for it in items:
                        by_platform.setdefault(it.platform, set()).add(it.external_id)
                    # Mark stale for every platform this source declares it can emit,
                    # so platforms with no items this run still get cleaned up.
                    declared = set(getattr(source_obj, "PLATFORMS", [r["name"]]))
                    for plat in declared:
                        db.mark_stale_external_items(
                            plat, iso_start, iso_end,
                            seen_ids=by_platform.get(plat, set()),
                        )
                    per_source_results[r["name"]] = {"ok": True, "count": len(items)}
                else:
                    entry = {"ok": False, "error": r["error"]}
                    if r.get("session_expired"):
                        entry["session_expired"] = True
                    per_source_results[r["name"]] = entry

        return {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "window": {"start": iso_start, "end": iso_end},
            "results": per_source_results,
        }
    finally:
        _LOCK.release()


def get_configured_sources() -> list:
    """Return the list of source modules to drive in production.

    Kept as a function (not a constant) so tests can swap in stubs without
    monkeypatching imports.
    """
    from core.refresh import (
        youtube_source,
        simplecast_source,
        rock_source,
        rock_email_source,
        vista_source,
    )
    return [youtube_source, simplecast_source, rock_source, rock_email_source, vista_source]
