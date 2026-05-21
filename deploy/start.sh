#!/usr/bin/env bash
# Launch the hosted stack: virtual display, VNC server, WS bridge, then Flask.
# Each helper runs in the background; Flask runs in the foreground as PID 1's
# child so container logs follow the app.
set -e

DISPLAY="${DISPLAY:-:99}"
export DISPLAY

# Virtual display the headed Chrome renders into.
Xvfb "$DISPLAY" -screen 0 1280x800x24 &

# Wait for the X socket so x11vnc doesn't race ahead of Xvfb.
sock="/tmp/.X11-unix/X${DISPLAY#:}"
for _ in $(seq 1 50); do
    [ -S "$sock" ] && break
    sleep 0.1
done

# The WebSocket gate lives on the VNC layer, not the proxy: Caddy's
# forward_auth breaks the WS upgrade, so instead x11vnc requires a VNC password
# and the (auth-gated) app hands it to noVNC. An unauthenticated hit on /vnc-ws
# reaches websockify but can't pass VNC auth. The password is generated once and
# persisted on the data volume; we export it so the Flask process inherits it.
VNC_PASS_FILE="${VNC_PASS_FILE:-/data/vnc_password}"
if [ ! -s "$VNC_PASS_FILE" ]; then
    mkdir -p "$(dirname "$VNC_PASS_FILE")"
    head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20 > "$VNC_PASS_FILE"
    chmod 600 "$VNC_PASS_FILE"
fi
export VNC_PASSWORD="$(cat "$VNC_PASS_FILE")"

# VNC server bound to loopback only — never expose 5900/6080 publicly.
x11vnc -display "$DISPLAY" -localhost -passwd "$VNC_PASSWORD" -forever -shared -rfbport 5900 &

# WebSocket bridge on 6080 (loopback; reached only via the proxy).
websockify --web=/usr/share/novnc 6080 localhost:5900 &

# Launch via `flask --app app run`, not `python app.py`: running app.py as
# __main__ makes blueprints.settings' `from app import _cached_yt_authenticated`
# re-import app.py as a *second* module, re-entering create_app() and crashing
# on a circular import. Importing `app` as a module (the path the tests use)
# loads it exactly once. --no-reload keeps it single-process for the in-memory
# SSE job store; threaded is on by default for the SSE streams.
exec python -m flask --app app run --host 0.0.0.0 --port 8080 --no-reload
