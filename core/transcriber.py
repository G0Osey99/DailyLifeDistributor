"""Local transcription using OpenAI's Whisper model (offline, no API key required)."""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading

logger = logging.getLogger(__name__)

# Module-level cache for loaded Whisper model
_model_cache: dict[str, object] = {}
_model_lock = threading.Lock()


def _load_config() -> dict:
    try:
        from core.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _find_ffmpeg() -> str | None:
    """Locate the ffmpeg executable on both Windows and macOS/Linux."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg

    if sys.platform == "win32":
        candidates = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/usr/local/bin/ffmpeg",       # Homebrew on Intel Mac
            "/opt/homebrew/bin/ffmpeg",    # Homebrew on Apple Silicon (M1/M2/M3)
            "/usr/bin/ffmpeg",
        ]
    else:
        candidates = ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]

    for path in candidates:
        if os.path.exists(path):
            return path
    return None


class LocalTranscriber:
    """Transcribe audio/video files locally using OpenAI Whisper."""

    def __init__(self, config: dict | None = None):
        if config is None:
            config = _load_config()
        whisper_cfg = config.get("whisper", {})
        self.model_name: str = whisper_cfg.get("model", "base")
        self.auto_transcribe: bool = whisper_cfg.get("auto_transcribe", True)
        self._model = None

        # Locate ffmpeg and inject its directory into PATH so Whisper can find it
        self.ffmpeg_path = _find_ffmpeg()
        if self.ffmpeg_path:
            ffmpeg_dir = os.path.dirname(self.ffmpeg_path)
            if ffmpeg_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
            logger.info("ffmpeg found at: %s", self.ffmpeg_path)
        else:
            logger.error(
                "ffmpeg not found — transcription will fail.\n"
                "  Windows: download from https://ffmpeg.org/download.html, "
                "extract to C:\\ffmpeg, add C:\\ffmpeg\\bin to system PATH\n"
                "  macOS:   run 'brew install ffmpeg'"
            )

    def _get_model(self):
        """Lazy-load the Whisper model on first use, cached as class attribute."""
        if self.model_name in _model_cache:
            return _model_cache[self.model_name]

        with _model_lock:
            # Double-check after acquiring lock
            if self.model_name in _model_cache:
                return _model_cache[self.model_name]

            try:
                from faster_whisper import WhisperModel

                # Use WHISPER_DOWNLOAD_ROOT env var if set (set by launch_mac.command)
                # Falls back to default Whisper cache (~/.cache/whisper) if not set
                cache_dir = os.environ.get("WHISPER_DOWNLOAD_ROOT", None)

                if cache_dir:
                    os.makedirs(cache_dir, exist_ok=True)
                    logger.info("Whisper cache: %s", cache_dir)

                logger.info("Loading Whisper model: %s", self.model_name)
                # Replace whisper.load_model() with:
                self._model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",      # int8 is fast on CPU, no LLVM needed
                    download_root=cache_dir   # same cache_dir logic as before
                )
                _model_cache[self.model_name] = self._model
                logger.info("Whisper model '%s' loaded successfully.", self.model_name)
                return self._model
            except ImportError:
                logger.error("faster-whisper is not installed. Run: pip install faster-whisper")
                raise
            except Exception as e:
                logger.error("Failed to load Whisper model '%s': %s", self.model_name, e)
                raise

    def transcribe(self, media_path: str) -> str:
        """Transcribe the first 30 seconds of an audio or video file and return the text.

        Extracts a 30-second mono 16kHz clip via ffmpeg before passing to Whisper,
        which is much faster than transcribing the full file.
        Returns empty string on any error.
        """
        if not media_path or not os.path.isfile(media_path):
            logger.warning("Transcribe called with invalid path: %s", media_path)
            return ""

        if not self.ffmpeg_path:
            logger.error("Cannot transcribe — ffmpeg not found.")
            return ""

        # Extract first 30 seconds to a temp file
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_path = tmp.name
        tmp.close()  # close before ffmpeg writes (required on Windows)

        try:
            cmd = [
                self.ffmpeg_path,
                "-i", media_path,
                "-t", "30",        # first 30 seconds only
                "-ar", "16000",    # 16 kHz mono — optimal for Whisper
                "-ac", "1",
                "-q:a", "0",
                "-y",              # overwrite if exists
                tmp_path,
            ]

            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(cmd, capture_output=True, timeout=60, **kwargs)

            if result.returncode != 0:
                # H9: cap stderr to avoid logging tens of megabytes of ffmpeg
                # noise when given a malformed input file.
                err_tail = result.stderr[-2000:].decode(errors="replace") if result.stderr else ""
                logger.error("ffmpeg extraction failed (rc=%s): %s", result.returncode, err_tail)
                os.unlink(tmp_path)
                return ""

            # Transcribe the extracted clip
            self._model = self._get_model()
            logger.info("Transcribing first 30s of: %s", media_path)
            # Watchdog: faster-whisper's transcribe() has no built-in timeout
            # and we've seen it wedge on corrupted/edge-case inputs, holding
            # _model_lock and stalling every concurrent caller. Run it in a
            # worker thread and abandon if it overruns. We can't kill the
            # underlying C++ thread, but at least the calling Flask/SSE
            # request returns instead of blocking forever.
            _result_box: dict = {}

            def _run():
                try:
                    segs, _info = self._model.transcribe(
                        tmp_path,
                        beam_size=5,
                        language="en",
                    )
                    _result_box["text"] = " ".join(s.text for s in segs).strip()
                except Exception as e:
                    _result_box["error"] = e

            t = threading.Thread(target=_run, name="whisper-transcribe", daemon=True)
            t.start()
            # 30s of audio at int8/base typically transcribes in <20s on CPU;
            # 180s gives slow USB / first-load headroom while bounding the
            # worst case so the lock can't be held indefinitely.
            t.join(timeout=180)
            if t.is_alive():
                logger.error("Whisper transcribe timed out after 180s for %s — abandoning thread", media_path)
                return ""
            if "error" in _result_box:
                raise _result_box["error"]
            text = _result_box.get("text", "")
            logger.info("Transcription complete (%d characters).", len(text))
            return text

        except Exception as e:
            logger.error("Transcription failed for %s: %s", media_path, e)
            return ""
        finally:
            try:
                os.unlink(tmp_path)
            except OSError as e:
                # H11: on Windows the killed-ffmpeg-on-timeout case can leave
                # the file briefly locked; record so a leak is diagnosable.
                logger.debug("tmp cleanup failed for %s: %s", tmp_path, e)


def is_whisper_available() -> bool:
    """Check if the faster-whisper package is importable."""
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False
