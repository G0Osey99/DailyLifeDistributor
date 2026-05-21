FROM python:3.12-slim

# Chromium (not Google Chrome) so the image builds on both arm64 and amd64 —
# Google ships no arm64 Linux Chrome. Playwright is pointed at this binary via
# the per-service *_CHROME_PATH env vars below; it disables the Chromium
# sandbox by default, so running headed inside the container works.
# Plus the virtual display (Xvfb), VNC server (x11vnc), and the WS bridge.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates ffmpeg fonts-liberation \
      xvfb x11vnc novnc websockify \
      chromium \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV HOSTED=true DISPLAY=:99 \
    SIMPLECAST_CHROME_PATH=/usr/bin/chromium \
    VISTA_SOCIAL_CHROME_PATH=/usr/bin/chromium \
    ROCK_CHROME_PATH=/usr/bin/chromium
EXPOSE 8080 6080
CMD ["bash", "deploy/start.sh"]
