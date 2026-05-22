"""llamafile local LLM: generates short title suggestions for YouTube Shorts."""

import hashlib
import json
import logging
import os
import threading
import time
import requests
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# Bounded LRU + TTL cache. The previous unbounded dict grew for the lifetime
# of the process — a long-running USB-resident server can rack up hundreds
# of unique transcripts and never release them. 256 entries × ~1 KB of titles
# is trivial, and 24h TTL means a re-run on the same media still benefits.
_CACHE_MAX_ENTRIES = 256
_CACHE_TTL_SECONDS = 24 * 60 * 60
_cache: "OrderedDict[str, tuple[float, list[str]]]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(key: str) -> Optional[list[str]]:
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if now - ts > _CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return value


def _cache_put(key: str, value: list[str]) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)

# OpenAI-compatible LLM endpoint for title suggestions. Defaults match the
# bundled llamafile (port 8081, model name "local", which llamafile ignores).
# Override for any other OpenAI-compatible backend — e.g. Ollama on a VPS:
#   LLM_BASE_URL=http://localhost:11434   LLM_MODEL=llama3.2
LLM_BASE_URL = (os.environ.get("LLM_BASE_URL") or "http://localhost:8081").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL") or "local"

# Back-compat alias: the /health probe and older imports reference this name.
LLAMAFILE_BASE_URL = LLM_BASE_URL

# Circuit breaker for the local LLM. When llamafile/Ollama is down, the health
# check below would otherwise eat 5 s on every call (and the completion POST up
# to 120 s) — across a batch of rows that piles up into a long, pointless wait.
# After a few consecutive failures the breaker opens and is_llamafile_running()
# returns False instantly, so callers fall back to the manual path without
# hammering a dead endpoint. It re-probes after the cooldown.
_LLM_BREAKER_NAME = "llm:title"
# Defaults; overridable via the `llm.circuit_breaker` config section.
_LLM_FAILURE_THRESHOLD = 3
_LLM_RECOVERY_TIMEOUT = 120.0
# Request/health HTTP timeouts and sampling params, overridable via `llm`.
_LLM_REQUEST_TIMEOUT = 120
_LLM_HEALTH_TIMEOUT = 5
_LLM_TEMPERATURE = 0.8
_LLM_MAX_TOKENS = 300


def _llm_breaker():
    from core.circuit_breaker import get_breaker
    cb = (_load_config().get("llm", {}) or {}).get("circuit_breaker", {}) or {}
    return get_breaker(
        _LLM_BREAKER_NAME,
        failure_threshold=int(cb.get("failure_threshold", _LLM_FAILURE_THRESHOLD)),
        recovery_timeout=float(cb.get("recovery_timeout_seconds", _LLM_RECOVERY_TIMEOUT)),
    )

def _get_transcript_hash(transcript: str) -> str:
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()

def _load_config() -> dict:
    from core.config import load_config
    return load_config()

def is_llamafile_running() -> bool:
    """Check if llamafile server is running and reachable.

    Treat any failure (including unexpected exceptions) as "not running" —
    a stale server or DNS hiccup should fail closed so callers fall back to
    the manual path rather than wedge on a doomed POST.
    """
    breaker = _llm_breaker()
    if not breaker.allow():
        logger.debug("llamafile circuit open — reporting not running without probing")
        return False
    health_timeout = (_load_config().get("llm", {}) or {}).get(
        "health_timeout_seconds", _LLM_HEALTH_TIMEOUT)
    try:
        r = requests.get(f"{LLM_BASE_URL}/v1/models", timeout=health_timeout)
        ok = r.status_code < 500
        if ok:
            breaker.record_success()
        else:
            breaker.record_failure()
        return ok
    except Exception as e:
        breaker.record_failure()
        logger.debug("llamafile health check failed: %s", e)
        return False

def generate_title_suggestions(
    transcript: Optional[str],
    num_suggestions: Optional[int] = None,
    model: Optional[str] = None,
) -> list[str]:
    if not transcript or not transcript.strip():
        return []

    if not is_llamafile_running():
        logger.error("llamafile is not running — should start automatically on launch")
        return []

    config = _load_config()
    llm_config = config.get("llm", {})
    if num_suggestions is None:
        num_suggestions = llm_config.get("num_title_suggestions", 5)

    cache_key = _get_transcript_hash(transcript)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    prompt = (
        "You are an expert social-media copywriter for a faith-based YouTube Shorts "
        "channel. The channel covers Christian topics: faith, scripture, prayer, "
        "spiritual growth, and everyday application of biblical principles. "
        "Titles are conversational, curiosity-driven, and speak directly to the viewer. "
        "They use rhetorical questions, incomplete sentences that create tension, "
        "or bold statements. Emoji use is encouraged — include 1 relevant emoji in "
        "most titles to add personality and energy.\n\n"
        "Examples of this channel's style:\n"
        "- 'Our actions are not what condemn us? 🤔'\n"
        "- 'Attacked by a BEE?! 🐝'\n"
        "- 'Will you choose to remove the filter?! 🕶️'\n"
        "- 'Has there been a time where you have felt behind?'\n"
        "- 'Let's detox our worries! 😌'\n"
        "- 'It is IMPOSSIBLE to please God without...'\n"
        "- 'Your faith is more precious than gold! 🏆'\n\n"
        f"Generate {num_suggestions} YouTube Shorts title options in this style. "
        f"Each must be under 60 characters. "
        f"Include at least: one question, one incomplete sentence ending with '...', "
        f"and one bold declarative statement.\n\n"
        f"Base them on this transcript:\n{transcript}\n\n"
        f"Return ONLY a valid JSON array of strings. "
        f"No explanation, no markdown, no preamble. "
        f'Example: ["Title one", "Title two", "Title three"]'
    )

    # H12: pre-bind so the JSONDecodeError handler below never sees an
    # unbound name when failure happens inside response.json() itself.
    text = ""
    try:
        logger.info("Calling llamafile transcript_length=%d", len(transcript))
        # One retry on a transient network error — the LLM server may be mid
        # (re)start. Content failures (bad JSON, 4xx) are NOT retried here.
        response = None
        last_exc = None
        for attempt in range(2):
            try:
                response = requests.post(
                    f"{LLM_BASE_URL}/v1/chat/completions",
                    json={
                        "model": LLM_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": llm_config.get("temperature", _LLM_TEMPERATURE),
                        "max_tokens": llm_config.get("max_tokens", _LLM_MAX_TOKENS),
                    },
                    timeout=llm_config.get("request_timeout_seconds", _LLM_REQUEST_TIMEOUT),
                )
                break
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_exc = e
                if attempt == 0:
                    logger.warning(
                        "llamafile request transient failure (attempt 1/2): "
                        "%s — retrying in 1s", e,
                    )
                    time.sleep(1.0)
        if response is None:
            _llm_breaker().record_failure()
            logger.error("llamafile request failed after retries: %s", last_exc)
            return []
        response.raise_for_status()
        # H12: validate the JSON shape before indexing — an unexpected payload
        # used to surface as a confusing KeyError/IndexError trace.
        payload = response.json()
        try:
            text = payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            logger.error("llamafile returned unexpected payload shape: %s | %r", e, payload)
            return []
        logger.info("llamafile raw response: %s", text[:200])

        # Strip markdown code fences if present
        if text.startswith("```"):
            # L12: split may produce <2 parts if the closing fence is missing.
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else parts[0].lstrip("`")
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        # Strip Llama special tokens that may appear in output
        text = text.replace("<|eot_id|>", "")
        text = text.replace("<|end_of_text|>", "")
        text = text.replace("<|start_header_id|>", "")
        text = text.replace("<|end_header_id|>", "")
        text = text.strip()

        titles = json.loads(text)
        if isinstance(titles, list) and all(isinstance(t, str) for t in titles):
            _llm_breaker().record_success()
            _cache_put(cache_key, titles)
            return titles
        return []

    except requests.exceptions.ConnectionError:
        _llm_breaker().record_failure()
        logger.error("llamafile connection failed — did it start correctly?")
        return []
    except json.JSONDecodeError as e:
        # A malformed body is a content problem, not an infra outage — leave
        # the breaker untouched so a single odd response doesn't disable it.
        logger.error("llamafile response was not valid JSON: %s | Raw: %s", e, text)
        return []
    except Exception as e:
        _llm_breaker().record_failure()
        logger.error("llamafile title generation failed: %s", e, exc_info=True)
        return []

def clear_cache():
    with _cache_lock:
        _cache.clear()
