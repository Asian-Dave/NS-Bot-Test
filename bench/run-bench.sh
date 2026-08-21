#!/bin/sh
# Xvfb virtual display: in this topology the browser window is always visible and
# always the active tab, so rAF can never be throttled (unlike a hidden host tab).
Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
sleep 2
export DISPLAY=:99
# Software WebGL is blocklisted by default in current Chromium. All three of these
# are needed: allow software GL for WebGL, select the SwiftShader ANGLE backend,
# and bypass the GPU blocklist entry.
timeout 60 chromium \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-dbus \
  --enable-unsafe-swiftshader \
  --use-gl=angle \
  --use-angle=swiftshader \
  --ignore-gpu-blocklist \
  --window-size=1200,900 \
  --autoplay-policy=no-user-gesture-required \
  "$BENCH_URL" 2>&1 | grep -iE 'blocklist|swiftshader|ContextResult|gpu' | head -8
echo "chromium-exited"
