"""Vista background-image gatherer for the Rock Daily Experience flow.

Pipeline (per spec):
    1. Ask the local llamafile (Llama 3.2) for 3 short visual search terms
       given the verse text.
    2. Search Unsplash (preferred — has `content_filter=high`) for each term;
       fall back to Pexels if Unsplash returns nothing usable.
    3. Filter out any photo used in the last 60 days, and any photo whose
       topic was used in the last 14 days, against the local
       `image_history` table in `state.db`.
    4. Trigger Unsplash's "download" ping (terms-of-service requirement, free).
    5. Download the chosen image to a NamedTemporaryFile and return its path
       along with attribution metadata.

Caller is responsible for:
    - Deleting the temp file in a `finally` block.
    - Calling `record_image_use(...)` from `core.db` ONLY after Rock has
      confirmed the image upload succeeded.

Required env vars (see .env):
    UNSPLASH_ACCESS_KEY    Required for the primary path.
    PEXELS_API_KEY         Optional fallback.

Failure model: returns None on any unrecoverable failure. The caller logs
a warning and skips the date (per project decision: fail+warn for the
no-image case, no bundled fallback).
"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import requests

from core import db as _db
from core.llm_title_gen import LLAMAFILE_BASE_URL, is_llamafile_running

log = logging.getLogger(__name__)


def _resolve_key(name: str) -> str:
    """Prefer the encrypted store; fall back to env during migration."""
    from core import secrets_store
    return (secrets_store.get_secret(name) or os.environ.get(name, "") or "").strip()


_UNSPLASH_SEARCH = "https://api.unsplash.com/search/photos"
_PEXELS_SEARCH = "https://api.pexels.com/v1/search"

_PHOTO_RECENCY_DAYS = 60
_TOPIC_RECENCY_DAYS = 14
_PER_PAGE = 10

# Cap a single image download at 25 MB. Stock photo "full" / "large2x" assets
# are typically 1-8 MB; anything larger is either a misconfigured CDN response
# or a hostile redirect, and we never want to fill up tmpfs on the USB drive.
_MAX_IMAGE_BYTES = 25 * 1024 * 1024


def _parse_retry_after(resp) -> Optional[float]:
    """Parse a Retry-After header (seconds-or-HTTP-date). None if absent/invalid."""
    raw = resp.headers.get("Retry-After") if resp is not None else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        # HTTP-date form is rare for these APIs; ignore.
        return None


@dataclass
class GatheredImage:
    """A downloaded image plus metadata needed to record + attribute it."""

    file_path: str          # NamedTemporaryFile path; caller deletes it
    photo_id: str           # stock-API id (string for portability)
    source: str             # "unsplash" or "pexels"
    topic: str              # the search term that hit
    photographer: str       # for credits log
    photo_url: str          # canonical web URL of the photo


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def gather_image_for_verse(
    verse_text: str,
    publish_date: date,
    *,
    topic_hint: str = "",
) -> Optional[GatheredImage]:
    """Return a GatheredImage for `verse_text`, or None if nothing suitable.

    `topic_hint` is an optional Excel "Topic" cell — fed to the LLM as
    extra context but not used as a search term directly.
    """
    terms = _topic_terms_for_verse(verse_text, topic_hint=topic_hint)
    if not terms:
        log.warning("Image gatherer: no topic terms produced for verse %r", verse_text[:60])
        return None

    photo_cutoff = (publish_date - timedelta(days=_PHOTO_RECENCY_DAYS)).isoformat()
    topic_cutoff = (publish_date - timedelta(days=_TOPIC_RECENCY_DAYS)).isoformat()
    recent_topics = _db.recent_topics(topic_cutoff)

    for term in terms:
        if term.lower() in recent_topics:
            log.info("Image gatherer: skipping topic %r (used within %dd)", term, _TOPIC_RECENCY_DAYS)
            continue

        # Unsplash first.
        result = _try_unsplash(term, photo_cutoff)
        if result is None:
            result = _try_pexels(term, photo_cutoff)
        if result is not None:
            return result

    log.warning("Image gatherer: no usable image found for any of %r", terms)
    return None


# ---------------------------------------------------------------------------
# Topic generation
# ---------------------------------------------------------------------------


def _topic_terms_for_verse(verse_text: str, *, topic_hint: str = "") -> list[str]:
    """Ask the local LLM for 3 short visual search terms. One retry on parse fail."""
    if not is_llamafile_running():
        log.error("Image gatherer: llamafile is not running")
        return []

    hint = f" Additional theme hint: {topic_hint}." if topic_hint else ""
    prompt = (
        "Given this Bible verse, return a JSON array of exactly 3 short "
        "visual search terms (1-2 words each) for a peaceful NATURE or "
        "LANDSCAPE photo to use as a background on a church website.\n\n"
        "Translate the verse's FEELING into an evocative outdoor scene — "
        "do NOT search for the literal concept, and do NOT reuse the "
        "example terms below verbatim. Generate fresh terms that fit "
        "THIS specific verse.\n\n"
        "Examples of the translation process (theme → possible terms):\n"
        "  - mercy / compassion → warm sunrise, autumn light, gentle stream\n"
        "  - strength / refuge → stone cliff, ancient oak, mountain ridge\n"
        "  - peace / rest → still water, quiet meadow, calm harbor\n"
        "  - joy / praise → wildflower field, bright dawn, sunlit leaves\n"
        "  - guidance / path → forest trail, lighthouse beam, winding road\n"
        "  - hope / renewal → spring blossom, fresh snow, clearing sky\n\n"
        "NEVER use any of these:\n"
        "  - Religious words: scripture, bible, cross, church, prayer, holy, "
        "faith, worship, temple, sacred, blessing, divine\n"
        "  - Any books, scrolls, candles, or written/printed material\n"
        "  - People, faces, hands, or bodies\n"
        "  - Dark, graphic, or violent imagery\n\n"
        "Return ONLY the JSON array — no prose, no markdown.\n\n"
        f"Verse: {verse_text}{hint}"
    )

    _MAX_LLM_ATTEMPTS = 2
    for attempt in range(1, _MAX_LLM_ATTEMPTS + 1):
        try:
            r = requests.post(
                f"{LLAMAFILE_BASE_URL}/v1/chat/completions",
                json={
                    "model": "local",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.5,
                    "max_tokens": 120,
                },
                timeout=60,
            )
            r.raise_for_status()
            payload = r.json()
            try:
                text = payload["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, TypeError) as e:
                log.warning("Image gatherer: LLM payload shape unexpected (attempt %d): %s | %r", attempt, e, payload)
                if attempt < _MAX_LLM_ATTEMPTS:
                    time.sleep((2 ** (attempt - 1)) + random.uniform(0, 0.5))
                continue
            text = _strip_llm_wrappers(text)
            terms = json.loads(text)
            if isinstance(terms, list) and all(isinstance(t, str) and t.strip() for t in terms):
                return [t.strip() for t in terms][:3]
            log.warning("Image gatherer: LLM returned non-list payload (attempt %d): %r", attempt, text[:120])
        except (requests.RequestException, json.JSONDecodeError) as e:
            log.warning("Image gatherer: LLM call failed (attempt %d): %s", attempt, e)
        # Exponential backoff with jitter between attempts so a struggling
        # llamafile gets a moment to recover instead of being hammered.
        if attempt < _MAX_LLM_ATTEMPTS:
            time.sleep((2 ** (attempt - 1)) + random.uniform(0, 0.5))
    return []


def _strip_llm_wrappers(text: str) -> str:
    """Strip code fences and Llama special tokens that occasionally leak in."""
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    for token in ("<|eot_id|>", "<|end_of_text|>", "<|start_header_id|>", "<|end_header_id|>"):
        text = text.replace(token, "")
    return text.strip()


# ---------------------------------------------------------------------------
# Unsplash
# ---------------------------------------------------------------------------


def _try_unsplash(term: str, photo_cutoff_iso: str) -> Optional[GatheredImage]:
    key = _resolve_key("UNSPLASH_ACCESS_KEY")
    if not key:
        log.debug("Image gatherer: UNSPLASH_ACCESS_KEY not set; skipping Unsplash for %r", term)
        return None

    headers = {"Authorization": f"Client-ID {key}", "Accept-Version": "v1"}
    params = {
        "query": term,
        "content_filter": "high",
        "orientation": "portrait",
        "per_page": _PER_PAGE,
    }
    r = _search_with_retry(_UNSPLASH_SEARCH, headers=headers, params=params, label="Unsplash", term=term)
    if r is None:
        return None

    used_ids = _db.recent_photo_ids("unsplash", photo_cutoff_iso)
    results = r.json().get("results") or []
    for photo in results:
        photo_id = str(photo.get("id") or "")
        if not photo_id or photo_id in used_ids:
            continue
        download_loc = (photo.get("links") or {}).get("download_location")
        # Required by Unsplash ToS — fire-and-forget; if it fails we still proceed.
        if download_loc:
            try:
                # M16: explicitly drain the body so the connection can be
                # released back to the pool on slow responses.
                _ping = requests.get(download_loc, headers=headers, timeout=10)
                _ping.close()
            except requests.RequestException as e:
                log.debug("Image gatherer: Unsplash download ping failed: %s", e)

        download_url = (photo.get("urls") or {}).get("full") or (photo.get("urls") or {}).get("regular")
        if not download_url:
            continue
        path = _download_to_temp(download_url)
        if path is None:
            continue
        photographer = ((photo.get("user") or {}).get("name") or "").strip()
        photo_url = (photo.get("links") or {}).get("html", "")
        log.info("Image gatherer: picked Unsplash %s for term %r", photo_id, term)
        return GatheredImage(
            file_path=path,
            photo_id=photo_id,
            source="unsplash",
            topic=term,
            photographer=photographer,
            photo_url=photo_url,
        )

    return None


# ---------------------------------------------------------------------------
# Pexels (fallback)
# ---------------------------------------------------------------------------


def _try_pexels(term: str, photo_cutoff_iso: str) -> Optional[GatheredImage]:
    key = _resolve_key("PEXELS_API_KEY")
    if not key:
        log.debug("Image gatherer: PEXELS_API_KEY not set; skipping Pexels for %r", term)
        return None

    headers = {"Authorization": key}
    params = {"query": term, "orientation": "portrait", "per_page": _PER_PAGE}
    r = _search_with_retry(_PEXELS_SEARCH, headers=headers, params=params, label="Pexels", term=term)
    if r is None:
        return None

    used_ids = _db.recent_photo_ids("pexels", photo_cutoff_iso)
    photos = r.json().get("photos") or []
    for photo in photos:
        photo_id = str(photo.get("id") or "")
        if not photo_id or photo_id in used_ids:
            continue
        src = photo.get("src") or {}
        download_url = src.get("large2x") or src.get("large") or src.get("original")
        if not download_url:
            continue
        path = _download_to_temp(download_url)
        if path is None:
            continue
        photographer = (photo.get("photographer") or "").strip()
        photo_url = photo.get("url", "")
        log.info("Image gatherer: picked Pexels %s for term %r", photo_id, term)
        return GatheredImage(
            file_path=path,
            photo_id=photo_id,
            source="pexels",
            topic=term,
            photographer=photographer,
            photo_url=photo_url,
        )

    return None


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------


def append_credits_entry(
    *,
    used_on_date: str,
    source: str,
    photographer: str,
    photo_url: str,
    topic: str,
) -> None:
    """Append a one-line credit to credits_<YYYY-MM>.txt at the project root.

    Tab-separated for easy paste into Excel later. Best-effort: never raises,
    so a filesystem hiccup can't fail an upload.
    """
    try:
        ym = used_on_date[:7] if len(used_on_date) >= 7 else "unknown"
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(project_root, f"credits_{ym}.txt")
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new_file:
                f.write("date\tsource\tphotographer\ttopic\turl\n")
            f.write(
                f"{used_on_date}\t{source}\t{photographer or '-'}\t"
                f"{topic or '-'}\t{photo_url or '-'}\n"
            )
    except OSError as e:
        log.warning("Image gatherer: credits log append failed: %s", e)


def _search_with_retry(url, *, headers, params, label: str, term: str):
    """GET a search endpoint with one retry on 429 / 5xx, honoring Retry-After.

    Returns the requests.Response (raise_for_status already passed) or None.
    Two attempts total — these are best-effort topical searches, not critical
    paths, so we don't want to spend more than ~30s of wall time on a flaky
    upstream when the caller has another term + another provider to try.
    """
    for attempt in (1, 2):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                wait = _parse_retry_after(r)
                if wait is None:
                    wait = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                # Cap so a misconfigured Retry-After can't park us for hours.
                wait = min(wait, 30.0)
                if attempt < 2:
                    log.warning(
                        "Image gatherer: %s returned %d for %r — retrying in %.1fs",
                        label, r.status_code, term, wait,
                    )
                    time.sleep(wait)
                    continue
                log.warning("Image gatherer: %s search failed for %r: HTTP %d", label, term, r.status_code)
                return None
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt < 2:
                wait = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                log.warning("Image gatherer: %s search transient error for %r: %s — retrying in %.1fs",
                            label, term, e, wait)
                time.sleep(wait)
                continue
            log.warning("Image gatherer: %s search failed for %r: %s", label, term, e)
            return None
    return None


def _download_to_temp(url: str) -> Optional[str]:
    # Track the partial file so we can clean it up on any failure path.
    tmp_path: Optional[str] = None
    try:
        with requests.get(url, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            # Cheap pre-check: if Content-Length is present and obviously
            # over our cap, bail before opening a temp file.
            try:
                cl = int(resp.headers.get("Content-Length", "") or 0)
            except ValueError:
                cl = 0
            if cl and cl > _MAX_IMAGE_BYTES:
                log.warning(
                    "Image gatherer: refusing %s — Content-Length %d exceeds cap %d",
                    url, cl, _MAX_IMAGE_BYTES,
                )
                return None
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp_path = tmp.name
            written = 0
            try:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > _MAX_IMAGE_BYTES:
                        # Hostile redirect or runaway response; abort + clean up.
                        log.warning(
                            "Image gatherer: aborting %s — exceeded %d bytes",
                            url, _MAX_IMAGE_BYTES,
                        )
                        tmp.close()
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        return None
                    tmp.write(chunk)
            finally:
                tmp.close()
            # Sanity: detect truncation against a known Content-Length.
            if cl and written < cl:
                log.warning(
                    "Image gatherer: truncated download for %s (got %d of %d bytes)",
                    url, written, cl,
                )
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                return None
            return tmp_path
    except (requests.RequestException, OSError) as e:
        log.warning("Image gatherer: download failed for %s: %s", url, e)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return None
