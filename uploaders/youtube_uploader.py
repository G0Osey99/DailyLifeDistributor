"""YouTube Data API v3 uploader for videos and Shorts."""

import os
import json
import logging
import random
import socket
import time
from datetime import timezone

try:
    import httplib2
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
except ImportError:
    httplib2 = None
    Request = None
    Credentials = None
    InstalledAppFlow = None
    build = None
    MediaFileUpload = None
    HttpError = Exception

# httplib2 default has NO socket timeout — a stalled connection can hang
# a worker thread indefinitely. 120s is generous enough for chunked uploads
# while still bounding the worst case so the thread pool can't be exhausted
# by zombies during a network blip.
_HTTP_TIMEOUT_SECONDS = 120

# Full youtube scope is used so the same token can both upload videos and
# manage them later (e.g. setting thumbnails on already-uploaded videos via
# the History tools). Upload-only would be enough for the upload+thumbnail
# pair we do here, but the broader scope avoids a re-auth if we ever expand.
SCOPES = ["https://www.googleapis.com/auth/youtube"]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOKEN_PATH   = os.path.join(_PROJECT_ROOT, "token.json")

_YT_CLIENT_SECRETS_NAME = "youtube.client_secrets"
_YT_TOKEN_NAME = "youtube.token"


def _load_token_json() -> str | None:
    """Return the stored token JSON, or None if not yet authorized."""
    from core import secrets_store
    return secrets_store.get_secret(_YT_TOKEN_NAME)


def _save_token_json(data: str) -> None:
    from core import secrets_store
    secrets_store.set_secret(_YT_TOKEN_NAME, data)


def _clear_token() -> None:
    from core import secrets_store
    secrets_store.delete_secret(_YT_TOKEN_NAME)


def _resolve_secrets_path() -> str:
    """Return absolute path to client_secrets.json.

    Honors YOUTUBE_CLIENT_SECRETS_PATH if set; absolute paths pass through
    unchanged, relative paths resolve against the project root. Read at
    call time (not import time) so changes via Settings take effect without
    a restart.
    """
    raw = os.environ.get("YOUTUBE_CLIENT_SECRETS_PATH", "client_secrets.json")
    return raw if os.path.isabs(raw) else os.path.join(_PROJECT_ROOT, raw)
CHUNK_SIZE = 1024 * 1024 * 5  # 5 MB
LARGE_FILE_WARNING_BYTES = 100 * 1024 * 1024  # 100 MB

_logger = logging.getLogger(__name__)


def _safe_callback(cb, *args, _what: str = "callback") -> None:
    """M1: invoke a progress/event callback without letting it derail the
    upload; log failures at debug level so a consistently-broken callback
    doesn't disappear into thin air."""
    if cb is None:
        return
    try:
        cb(*args)
    except Exception:
        _logger.debug("%s raised", _what, exc_info=True)

# Transient errors during a resumable upload — Google's docs explicitly
# recommend retrying these with exponential backoff rather than failing
# the whole upload. 5xx are server-side hiccups; 408/429 are throttling.
_RETRYABLE_STATUS = {500, 502, 503, 504, 408, 429}
_MAX_RETRIES = 5

logger = logging.getLogger(__name__)


def _is_retryable_http_error(err) -> bool:
    """Return True if a googleapiclient HttpError is worth retrying."""
    if HttpError is Exception or not isinstance(err, HttpError):
        return False
    status = getattr(getattr(err, "resp", None), "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return status in _RETRYABLE_STATUS


def _next_chunk_with_retry(request):
    """Wrapper around request.next_chunk() that retries transient failures.

    Network errors (ConnectionError, socket.error) and retryable HTTP status
    codes (5xx, 408, 429) get exponential backoff with jitter. Anything else
    propagates so the caller can surface a real error.
    """
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            return request.next_chunk()
        except HttpError as e:
            if not _is_retryable_http_error(e):
                raise
            last_exc = e
        except (ConnectionError, socket.error, TimeoutError) as e:
            last_exc = e
        sleep_for = (2 ** attempt) + random.uniform(0, 1)
        logger.warning(
            "YouTube chunk upload transient failure (attempt %d/%d): %s — "
            "retrying in %.1fs",
            attempt + 1, _MAX_RETRIES, last_exc, sleep_for,
        )
        time.sleep(sleep_for)
    raise last_exc  # type: ignore[misc]


def _load_config() -> dict:
    from core.config import load_config
    return load_config()


def _get_token_path() -> str:
    """Return path to token.json, resolved relative to the project root."""
    return _TOKEN_PATH


def _atomic_write_text(path: str, data: str) -> None:
    """Write `data` to `path` atomically.

    Why: token.json is on a USB drive. A non-atomic write that gets
    interrupted (USB unplug, power loss, concurrent refresh) leaves a
    truncated/empty file and the next run treats the user as unauthenticated.
    write-then-replace makes the swap atomic on the same filesystem.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def get_authenticated_service():
    """Build and return an authenticated YouTube API service.

    On first run, launches OAuth2 browser flow.
    On subsequent runs, loads and refreshes token.json automatically.
    """
    if build is None:
        raise ImportError("google-api-python-client is required but not installed")

    creds = None

    token_json = _load_token_json()
    if token_json:
        try:
            from google.oauth2.credentials import Credentials as _Creds
            creds = _Creds.from_authorized_user_info(json.loads(token_json), SCOPES)
        except (json.JSONDecodeError, ValueError) as e:
            # M26: corrupt token — surface a clear, actionable message.
            raise RuntimeError(
                f"Stored YouTube token is corrupt ({e}). Click 'Clear YouTube Token' in "
                "Settings, then re-authenticate."
            ) from e
        required_scope = "https://www.googleapis.com/auth/youtube"
        granted = getattr(creds, "scopes", None) or []
        if required_scope not in granted:
            logger.warning("Stored token missing required scope — clearing and re-authenticating")
            _clear_token()
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                # M19: refresh-token revoked / expired — clean up and surface a
                # message the user can act on.
                try:
                    from google.auth.exceptions import RefreshError as _RefreshError
                    is_refresh_err = isinstance(e, _RefreshError)
                except ImportError:
                    is_refresh_err = "invalid_grant" in str(e).lower() or "refresh" in str(e).lower()
                if is_refresh_err:
                    logger.warning("YouTube refresh failed — clearing stored token: %s", e)
                    _clear_token()
                    raise RuntimeError(
                        "YouTube token has been revoked or expired. "
                        "Re-authenticate in Settings."
                    ) from e
                raise
        else:
            # First-run OAuth opens a browser and blocks on a local HTTP
            # callback. If that runs inside the upload thread pool, the
            # worker freezes and SSE goes silent until the user happens to
            # see (and complete) the popup. Refuse to start it from a
            # background thread; force the user to authenticate via the
            # Settings page (which runs on the main request thread) first.
            import threading as _threading
            if _threading.current_thread() is not _threading.main_thread():
                raise RuntimeError(
                    "YouTube is not authenticated and OAuth cannot run from a "
                    "background upload thread. Open Settings and click "
                    "'Connect YouTube' to authenticate, then retry the upload."
                )
            from core import secrets_store as _ss
            with _ss.materialize_blob_to_tempfile(
                _YT_CLIENT_SECRETS_NAME, suffix=".json"
            ) as stored_path:
                secrets_path = stored_path or _resolve_secrets_path()
                if not os.path.exists(secrets_path):
                    raise FileNotFoundError(
                        f"Client secrets file not found: {secrets_path}. "
                        "Download it from Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
                creds = flow.run_local_server(port=0)

        _save_token_json(creds.to_json())

    # Wrap creds in an authorized httplib2.Http with a socket timeout so
    # a stalled chunk read can't pin a worker thread forever. Without this
    # the library defaults to no timeout.
    if httplib2 is not None:
        try:
            from google_auth_httplib2 import AuthorizedHttp  # type: ignore
            authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS))
            return build(API_SERVICE_NAME, API_VERSION, http=authed_http, cache_discovery=False)
        except ImportError:
            # google-auth-httplib2 isn't installed — fall back to creds path.
            # Sets a process-wide default socket timeout as a backstop.
            socket.setdefaulttimeout(_HTTP_TIMEOUT_SECONDS)
    return build(API_SERVICE_NAME, API_VERSION, credentials=creds, cache_discovery=False)


def is_authenticated() -> bool:
    """Return True if we have credentials that are usable now or refreshable.

    Treats expired-but-refreshable tokens as authenticated, since
    `get_authenticated_service()` will refresh them transparently before
    making any API call. Without this, the navbar's "Not authenticated"
    badge fires every ~hour even though uploads still work.

    This method never triggers OAuth flow and never refreshes tokens.
    """
    if Credentials is None:
        return False

    token_json = _load_token_json()
    if not token_json:
        return False

    try:
        from google.oauth2.credentials import Credentials as _Creds
        import json as _json
        creds = _Creds.from_authorized_user_info(_json.loads(token_json), SCOPES)
        if creds is None:
            return False
        if creds.valid:
            return True
        # Expired but holds a refresh token → still effectively authed.
        if creds.expired and getattr(creds, "refresh_token", None):
            return True
        return False
    except Exception:
        logger.debug("is_authenticated() failed; treating as unauthenticated", exc_info=True)
        return False


def handle_quota_error(error) -> dict:
    """Return a structured error dict for quota exceeded errors."""
    return {
        "video_id": None,
        "url": None,
        "scheduled_time": None,
        "success": False,
        "error": f"YouTube API quota exceeded: {str(error)}",
    }




def upload_video(entry, is_short: bool = False, dry_run: bool = False, elements=None,
                 progress_callback=None, event_callback=None) -> dict:
    """Upload a video or Short to YouTube.

    Args:
        entry: A ReviewEntry dataclass instance.
        is_short: If True, upload as a YouTube Short.
        dry_run: If True, log payload details and skip all API calls.
        elements: An UploadElements instance controlling which elements to include.
        progress_callback: Optional callable(percent, bytes_sent, bytes_total, eta_seconds)
            called after each chunk upload to report progress.

    Returns:
        Dict with: video_id, url, scheduled_time, success, error
    """
    result = {
        "video_id": None,
        "url": None,
        "scheduled_time": None,
        "success": False,
        "error": None,
    }

    try:
        # Check if the platform is disabled via elements
        if elements is not None:
            if is_short and not elements.yt_shorts_enabled:
                return {"skipped": True, "success": True, "error": None}
            if not is_short and not elements.yt_video_enabled:
                return {"skipped": True, "success": True, "error": None}

        config = _load_config()
        yt_config = config.get("youtube", {})

        # Select file path and title based on type
        if is_short:
            file_path = entry.youtube_shorts_path
            schedule_dt = entry.shorts_schedule_dt
            # Respect elements title flag
            if elements is not None and not elements.yt_shorts_title:
                title = os.path.splitext(os.path.basename(file_path or ""))[0]
            else:
                title = entry.youtube_shorts_title or ""
                # Shorts title fallback chain
                if not title:
                    if entry.youtube_title:
                        title = entry.youtube_title
                        logger.info("Shorts title fell back to: %s", title)
                    elif file_path:
                        title = os.path.splitext(os.path.basename(file_path))[0]
                        logger.info("Shorts title fell back to: %s", title)
        else:
            file_path = entry.youtube_video_path
            schedule_dt = entry.youtube_schedule_dt
            # Respect elements title flag
            if elements is not None and not elements.yt_video_title:
                title = os.path.splitext(os.path.basename(file_path or ""))[0]
            else:
                title = entry.youtube_title

        if not file_path or not os.path.isfile(file_path):
            result["error"] = f"Video file not found: {file_path}"
            return result

        # Never send an empty title — YouTube will reject it
        if not title:
            if file_path:
                title = os.path.splitext(os.path.basename(file_path))[0]
                logger.info("Title fell back to filename: %s", title)
            else:
                result["error"] = "No title provided for upload"
                return result

        file_size_bytes = os.path.getsize(file_path)
        if file_size_bytes > LARGE_FILE_WARNING_BYTES:
            logger.warning(
                "Large YouTube upload (%s bytes > 100MB) for file: %s",
                file_size_bytes,
                file_path,
            )

        # Build description — respect elements flag
        if elements is not None:
            desc_flag = elements.yt_shorts_description if is_short else elements.yt_video_description
        else:
            desc_flag = True
        description = (entry.description or "") if desc_flag else ""
        if is_short and "#Shorts" not in description:
            description = f"{description}\n#Shorts".strip()

        # Append description footer if configured
        footer_key = "youtube_shorts" if is_short else "youtube_video"
        footer = config.get("description_footers", {}).get(footer_key, "")
        if footer and footer.strip():
            description = description + "\n\n" + footer.strip()

        # Tags — respect elements flag
        if elements is not None:
            tags_flag = elements.yt_shorts_tags if is_short else elements.yt_video_tags
        else:
            tags_flag = True
        tags = (entry.tags or []) if tags_flag else []

        # Schedule — respect elements flag
        if elements is not None:
            sched_flag = elements.yt_shorts_schedule if is_short else elements.yt_video_schedule
        else:
            sched_flag = True

        publish_at = None
        if sched_flag and schedule_dt:
            utc_dt = schedule_dt.astimezone(timezone.utc)
            publish_at = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.0Z")

        # Build request body
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": yt_config.get("default_category_id", "22"),
            },
            "status": {
                "privacyStatus": yt_config.get("default_privacy", "private"),
                "madeForKids": yt_config.get("made_for_kids", False),
                "selfDeclaredMadeForKids": yt_config.get("made_for_kids", False),
            },
        }

        if publish_at:
            body["status"]["publishAt"] = publish_at
            body["status"]["privacyStatus"] = "private"
        elif not sched_flag:
            # No schedule → force private draft
            body["status"]["privacyStatus"] = "private"

        logger.info(
            "YouTube upload configured with resumable upload enabled (chunk size: %s bytes)",
            CHUNK_SIZE,
        )

        if dry_run:
            logger.info(
                "YouTube upload dry-run; no API calls will be made. Payload: %s",
                json.dumps(
                    {
                        "is_short": is_short,
                        "file_path": file_path,
                        "file_size_bytes": file_size_bytes,
                        "title": title,
                        "description": description,
                        "tags": tags,
                        "publish_at": publish_at,
                        "thumbnail_path": entry.thumbnail_path,
                        "request_body": body,
                        "resumable": True,
                        "chunk_size_bytes": CHUNK_SIZE,
                    },
                    default=str,
                ),
            )
            result["scheduled_time"] = publish_at
            result["success"] = True
            return result

        # Authenticate and upload
        youtube = get_authenticated_service()

        media = MediaFileUpload(
            file_path,
            chunksize=CHUNK_SIZE,
            resumable=True,
        )

        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        logger.info("Uploading %s: %s", "Short" if is_short else "video", title)
        upload_start = time.time()
        while response is None:
            status, response = _next_chunk_with_retry(request)
            if status:
                progress = int(status.progress() * 100)
                bytes_sent = int(status.resumable_progress)
                bytes_total = int(status.total_size) if status.total_size else file_size_bytes
                elapsed = time.time() - upload_start
                eta_seconds = None
                if elapsed > 0 and bytes_sent > 0:
                    speed = bytes_sent / elapsed
                    eta_seconds = int((bytes_total - bytes_sent) / speed) if speed > 0 else None
                logger.info("  Upload progress: %d%%", progress)
                _safe_callback(progress_callback, progress, bytes_sent, bytes_total, eta_seconds, _what="progress_callback")

        video_id = response["id"]
        logger.info("  Upload complete! Video ID: %s", video_id)
        if is_short:
            video_url = f"https://www.youtube.com/shorts/{video_id}"
        else:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        # H3: lock in success state IMMEDIATELY now that the video exists on
        # YouTube. Any later exception (thumbnail, callback, quota tracking)
        # must NOT flip success back to False — that would leave an orphan
        # video on YouTube with no DB record. Errors after this point only
        # annotate result["error"]; success stays True.
        result["video_id"] = video_id
        result["url"] = video_url
        result["scheduled_time"] = publish_at
        result["success"] = True

        # Charge quota now that videos.insert has actually returned an id.
        # Charging in the SSE consumer (the previous home for this) billed
        # 1600 units even when the API returned a 4xx that we surfaced as
        # an error row.
        try:
            from core.quota import track_quota_usage
            track_quota_usage("shorts_upload" if is_short else "video_upload")
        except Exception as e:
            logger.warning("quota tracking failed for video upload: %s", e)
        _safe_callback(progress_callback, 100, file_size_bytes, file_size_bytes, 0, _what="progress_callback(final)")

        logger.info("Upload complete for %s — setting thumbnail immediately", video_id)
        _safe_callback(event_callback, {"type": "upload_progress", "percent": 100}, _what="event_callback(upload_progress)")

        # Set thumbnail — respect elements flag
        thumb_flag = True
        if elements is not None:
            thumb_flag = elements.yt_shorts_thumbnail if is_short else elements.yt_video_thumbnail

        if thumb_flag and entry.thumbnail_path and os.path.isfile(entry.thumbnail_path):
            try:
                # Retry transient failures (5xx/408/429, network blips). The
                # video is already uploaded; a single network glitch on the
                # thumbnail request shouldn't lose the artwork.
                thumb_last_exc = None
                for thumb_attempt in range(3):
                    try:
                        thumb_media = MediaFileUpload(entry.thumbnail_path)
                        youtube.thumbnails().set(
                            videoId=video_id,
                            media_body=thumb_media,
                        ).execute()
                        thumb_last_exc = None
                        break
                    except HttpError as te:
                        if not _is_retryable_http_error(te):
                            raise
                        thumb_last_exc = te
                    except (ConnectionError, socket.error, TimeoutError) as te:
                        thumb_last_exc = te
                    sleep_for = (2 ** thumb_attempt) + random.uniform(0, 1)
                    logger.warning(
                        "Thumbnail set transient failure (attempt %d/3): %s — retrying in %.1fs",
                        thumb_attempt + 1, thumb_last_exc, sleep_for,
                    )
                    time.sleep(sleep_for)
                if thumb_last_exc is not None:
                    raise thumb_last_exc
                logger.info("Thumbnail set for video %s", video_id)
                try:
                    from core.quota import track_quota_usage
                    track_quota_usage("shorts_thumbnail" if is_short else "thumbnail_set")
                except Exception as qe:
                    logger.warning("quota tracking failed for thumbnail set: %s", qe)
                _safe_callback(event_callback, {"type": "thumbnail_set", "video_id": video_id}, _what="event_callback(thumbnail_set)")
            except Exception as e:
                logger.warning("Failed to set thumbnail for %s: %s", video_id, e)
                _safe_callback(event_callback, {"type": "thumbnail_failed", "video_id": video_id, "message": str(e)}, _what="event_callback(thumbnail_failed)")
        else:
            _safe_callback(event_callback, {"type": "thumbnail_skipped", "video_id": video_id}, _what="event_callback(thumbnail_skipped)")

    except HttpError as e:
        if "quotaExceeded" in str(e) or "quota" in str(e).lower():
            return handle_quota_error(e)
        result["error"] = f"YouTube API error: {str(e)}"
    except Exception as e:
        result["error"] = f"Upload failed: {str(e)}"

    return result
