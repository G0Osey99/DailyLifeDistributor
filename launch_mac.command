#!/bin/bash
# ─────────────────────────────────────────────
#  Media Uploader — Mac Launch Script
#  Double-click this file in Finder to start
# ─────────────────────────────────────────────

USB_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$USB_DIR"

cleanup() {
    echo ""
    echo "Shutting down..."
    if [ -n "$LLAMAFILE_PID" ] && kill -0 "$LLAMAFILE_PID" 2>/dev/null; then
        kill "$LLAMAFILE_PID" 2>/dev/null
        echo "✓ llamafile stopped"
    fi
    pkill -f "llamafile" 2>/dev/null
    exit 0
}
trap cleanup EXIT INT TERM

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Media Uploader"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Detect Mac architecture ──
ARCH=$(uname -m)

# ── Python: bundled first, system fallback ──
# Find the first python3* binary in a given bin directory
find_bundled_python() {
    local bin_dir="$1/bin"
    # Prefer the most specific match (e.g. python3.12 over python3)
    local found
    found=$(find "$bin_dir" -maxdepth 1 -name "python3*" -type f 2>/dev/null | sort -V | tail -1)
    echo "$found"
}

ARM_PYTHON=$(find_bundled_python "$USB_DIR/bin/python_arm")
INTEL_PYTHON=$(find_bundled_python "$USB_DIR/bin/python_intel")

if [ "$ARCH" = "arm64" ] && [ -n "$ARM_PYTHON" ]; then
    PYTHON="$ARM_PYTHON"
    xattr -rd com.apple.quarantine "$USB_DIR/bin/python_arm" 2>/dev/null
    echo "✓ Using bundled Python (Apple Silicon): $(basename "$PYTHON")"

elif [ "$ARCH" = "x86_64" ] && [ -n "$INTEL_PYTHON" ]; then
    PYTHON="$INTEL_PYTHON"
    xattr -rd com.apple.quarantine "$USB_DIR/bin/python_intel" 2>/dev/null
    echo "✓ Using bundled Python (Intel Mac): $(basename "$PYTHON")"

elif command -v python3 &>/dev/null; then
    PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
    if [ -n "$PY_MINOR" ] && [ "$PY_MINOR" -ge 11 ]; then
        PYTHON="python3.12"
        echo "✓ Using system Python ($(python3 --version))"
    else
        echo "✗ System Python too old (need 3.11+, found $(python3 --version 2>&1))"
        echo "  Place bundled Python in $USB_DIR/python_arm/ or python_intel/"
        read -p "Press Enter to exit..."
        exit 1
    fi
else
    echo "✗ No Python found."
    echo "  Place bundled Python in:"
    echo "    Apple Silicon: $USB_DIR/python_arm/"
    echo "    Intel Mac:     $USB_DIR/python_intel/"
    read -p "Press Enter to exit..."
    exit 1
fi

# ── ffmpeg ──
if [ -f "$USB_DIR/bin/ffmpeg" ]; then
    export PATH="$USB_DIR/bin:$PATH"
    xattr -d com.apple.quarantine "$USB_DIR/bin/ffmpeg" 2>/dev/null
    echo "✓ Using bundled ffmpeg"
elif command -v ffmpeg &>/dev/null; then
    echo "✓ Using system ffmpeg"
else
    echo "⚠ ffmpeg not found — transcription will not work"
    echo "  Download static binary from https://evermeet.cx/ffmpeg/"
    echo "  Place at: $USB_DIR/bin/ffmpeg"
fi

# ── llamafile: single universal binary ──
LLAMAFILE_BIN=""
if [ -f "$USB_DIR/bin/llamafile" ]; then
    LLAMAFILE_BIN="$USB_DIR/bin/llamafile"
    xattr -d com.apple.quarantine "$USB_DIR/bin/llamafile" 2>/dev/null
    chmod +x "$USB_DIR/bin/llamafile" 2>/dev/null
    echo "✓ Using llamafile"
else
    echo "⚠ llamafile not found — AI title generation unavailable"
    echo "  Download from https://github.com/Mozilla-Ocho/llamafile/releases"
fi

LLAMAFILE_MODEL="$USB_DIR/bin/ollama_models/llama3.2.gguf"

if [ -n "$LLAMAFILE_BIN" ] && [ -f "$LLAMAFILE_MODEL" ]; then
    echo "Starting llamafile server..."
    "$LLAMAFILE_BIN" \
        --model "$LLAMAFILE_MODEL" \
        --server \
        --host 127.0.0.1 \
        --port 8081 \
        --nobrowser \
        --log-disable \
        &>/dev/null &
    LLAMAFILE_PID=$!

    sleep 2

    if kill -0 "$LLAMAFILE_PID" 2>/dev/null; then
        echo "✓ llamafile started (model loading in background)"
    else
        echo "⚠ llamafile crashed on startup — AI titles unavailable"
        LLAMAFILE_PID=""
    fi
elif [ -n "$LLAMAFILE_BIN" ] && [ ! -f "$LLAMAFILE_MODEL" ]; then
    echo "⚠ Model file not found at $LLAMAFILE_MODEL"
    echo "  Download Llama-3.2-3B-Instruct-Q4_K_M.gguf from HuggingFace"
fi

# ── Virtual environment ──
VENV="$USB_DIR/venv"
if [ ! -d "$VENV" ]; then
    echo ""
    echo "First run — setting up (2-3 minutes, once only)..."
    echo ""
    "$PYTHON" -m venv "$VENV"
    if [ $? -ne 0 ]; then
        echo "✗ Failed to create virtual environment"
        read -p "Press Enter to exit..."
        exit 1
    fi
    source "$VENV/bin/activate"
    # Upgrade pip within the venv only
    "$VENV/bin/pip" install --upgrade pip --quiet --isolated

    # Install all packages to venv on USB, isolated from system
    "$VENV/bin/pip" install \
        --isolated \
        --require-virtualenv \
        -r "$USB_DIR/requirements.txt"
    if [ $? -ne 0 ]; then
        echo "✗ Failed to install dependencies"
        read -p "Press Enter to exit..."
        exit 1
    fi
    echo "✓ Setup complete"
else
    source "$VENV/bin/activate"
fi

# ── Environment variables ──
export WHISPER_DOWNLOAD_ROOT="$USB_DIR/bin/whisper_cache"
export FLASK_APP="app"
export FLASK_ENV="production"
export SIMPLECAST_HEADLESS="true"
export ROCK_HEADLESS="true"
export VISTA_SOCIAL_HEADLESS="true"

# ── Open browser after short delay ──
(sleep 2 && open "http://localhost:8080") &

echo ""
echo "✓ Starting at http://localhost:8080"
echo "  Press Ctrl+C to stop"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

flask run --host=127.0.0.1 --port=8080
