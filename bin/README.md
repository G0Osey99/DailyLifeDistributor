# `bin/` — host-provisioned binaries, runtimes & models

Nothing in this directory is committed to git (see the repo `.gitignore`):
it's ~3 GB of platform binaries, language runtimes, and model weights that
are installed/downloaded per host machine, not versioned. Only this README
is tracked, to document what needs to be here.

The `launch_mac.command` script auto-detects architecture (arm vs intel) and
expects the following layout:

| Path | What it is | ~Size | How to provision |
|------|------------|-------|------------------|
| `bin/ffmpeg` | ffmpeg binary (Whisper audio extraction; falls back to system ffmpeg) | 77 MB | Download a static build from ffmpeg.org, or `brew install ffmpeg` and symlink |
| `bin/llamafile` | llamafile LLM server (llama3.2) for Shorts title suggestions; listens on port 8081 | 294 MB | Download from the llamafile releases |
| `bin/ollama_models/llama3.2.gguf` | llama3.2 model weights | 1.9 GB | `ollama pull llama3.2`, or download the `.gguf` |
| `bin/whisper_cache/` | faster-whisper model cache (`base.pt` + HF blobs) | 280 MB | Auto-downloaded on first transcription run; or pre-seed the HF cache |
| `bin/node_arm/`, `bin/node_intel/` | bundled Node runtimes (arm64 / x64) | ~190 MB each | Download Node LTS for each arch, or use a system Node |
| `bin/python_arm/`, `bin/python_intel/` | bundled CPython 3.11+ environments (arm64 / x64) | ~55 MB each | Build/extract a relocatable CPython per arch, then `pip install -r requirements.txt` into it |

For a plain-Python run on any platform you don't need most of this — just
`pip install -r requirements.txt` and `python app.py` (see CLAUDE.md). The
bundled `bin/` layout is only what `launch_mac.command` uses on the
production Mac.
