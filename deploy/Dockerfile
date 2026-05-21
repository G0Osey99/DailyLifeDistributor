FROM python:3.12-slim

# Chrome + virtual display + VNC bridge
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget gnupg ca-certificates ffmpeg \
      xvfb x11vnc novnc websockify \
 && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y /tmp/chrome.deb && rm /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV HOSTED=true DISPLAY=:99
EXPOSE 8080 6080
CMD ["bash", "deploy/start.sh"]
