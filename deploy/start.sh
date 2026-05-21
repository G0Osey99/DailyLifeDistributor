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

# VNC server bound to loopback only — never expose 5900/6080 publicly; the
# reverse proxy (deploy/Caddyfile) gates /vnc-ws with forward_auth.
x11vnc -display "$DISPLAY" -localhost -nopw -forever -shared -rfbport 5900 &

# WebSocket bridge on 6080 (loopback; reached only via the proxy).
websockify --web=/usr/share/novnc 6080 localhost:5900 &

exec python app.py
