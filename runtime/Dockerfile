# Bot runtime image (arm64 / Apple Silicon).
# No Google Chrome exists for Linux arm64, so Chromium. Ruffle is WASM, so fine.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium xvfb x11vnc novnc websockify python3 \
      python3-numpy python3-opencv \
      ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY cdp.py measure_ruffle.py /engine/
COPY start.sh /start.sh
RUN chmod +x /start.sh
ENV GAME_URL="https://ninjasaga.cc/play"
# 6080 = noVNC (browser-based VNC). 9222 = CDP, bound to localhost inside the
# container only - the bot talks to it from inside, it is never published.
EXPOSE 6080
CMD ["/start.sh"]
