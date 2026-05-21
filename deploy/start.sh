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

# NOTE: x11vnc is NOT started here. The app (core/vnc.py) starts/stops it per
# remote-login session with a fresh, single-use VNC password (option 2), so a
# leaked password is useless beyond its session and there's no live VNC server
# when nobody is logging in. websockify stays up and simply has nothing to
# bridge to (and rejects /vnc-ws) until a session starts x11vnc on :5900.
mkdir -p /data

# WebSocket bridge on 6080 (loopback; reached only via the proxy). It connects
# to :5900 on demand — x11vnc is brought up there per session by the app.
websockify --web=/usr/share/novnc 6080 localhost:5900 &

# Launch via `flask --app app run`, not `python app.py`: running app.py as
# __main__ makes blueprints.settings' `from app import _cached_yt_authenticated`
# re-import app.py as a *second* module, re-entering create_app() and crashing
# on a circular import. Importing `app` as a module (the path the tests use)
# loads it exactly once. --no-reload keeps it single-process for the in-memory
# SSE job store; threaded is on by default for the SSE streams.
exec python -m flask --app app run --host 0.0.0.0 --port 8080 --no-reload
