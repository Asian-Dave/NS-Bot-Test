#!/bin/sh
set -e
PROFILE=${PROFILE:-/profile}
mkdir -p "$PROFILE"

# Clear stale singleton locks. `docker rm -f` SIGKILLs Chromium, so it never gets
# to release these; the next container has a different hostname and Chromium then
# refuses to start, claiming the profile is in use "on another computer". This
# container is the only user of the volume, so clearing them is always safe.
rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonCookie" "$PROFILE/SingletonSocket"

# --- virtual display -------------------------------------------------------
# In this topology the browser window is always visible and always the active
# tab, so Ruffle's requestAnimationFrame loop can never be throttled. A hidden
# host tab delivers 0 rAF callbacks; here it always renders.
Xvfb :99 -screen 0 1400x1100x24 -nolisten tcp &
sleep 2
export DISPLAY=:99

# --- browser ---------------------------------------------------------------
# The three SwiftShader flags are mandatory: without them current Chromium
# reports "WebGL1 blocklisted" and Ruffle silently drops to its much slower
# canvas2d renderer.
chromium \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-dbus \
  --enable-unsafe-swiftshader \
  --use-gl=angle \
  --use-angle=swiftshader \
  --ignore-gpu-blocklist \
  --remote-debugging-port=9222 \
  --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$PROFILE" \
  --no-first-run --no-default-browser-check \
  --window-position=0,0 --window-size=1390,1090 \
  --autoplay-policy=no-user-gesture-required \
  --disable-gpu-vsync \
  --disable-frame-rate-limit \
  "$GAME_URL" >/tmp/chromium.log 2>&1 &

# --- remote viewing --------------------------------------------------------
sleep 3
x11vnc -display :99 -nopw -forever -shared -rfbport 5900 -quiet >/tmp/x11vnc.log 2>&1 &
sleep 1
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &

echo "=============================================="
echo " noVNC ready:  http://localhost:6080/vnc.html"
echo " profile persisted at: $PROFILE"
echo "=============================================="
# keep the container alive
tail -f /dev/null
