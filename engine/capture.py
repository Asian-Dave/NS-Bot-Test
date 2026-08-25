"""Capture layer — page pixels via CDP, decoded for OpenCV.

Not mss. We never capture a screen, which is why the host needs no Screen
Recording permission and why the game window does not have to be frontmost.
Page.captureScreenshot composites the Ruffle WebGL canvas directly.

Coordinate note: inside the container devicePixelRatio is 1, so captured pixels
and CSS click coordinates are 1:1. On a Retina host it is 2, and captured pixels
are twice the click coordinates. `self.dpr` records which world we are in so
callers never have to guess.
"""
import cv2
import numpy as np


class Capture:
    def __init__(self, cdp):
        self.cdp = cdp
        self.dpr = float(cdp.evaluate("window.devicePixelRatio") or 1)
        vp = cdp.evaluate("JSON.stringify({w: innerWidth, h: innerHeight})")
        import json
        vp = json.loads(vp)
        self.viewport = (vp["w"], vp["h"])

    def frame(self, region=None, gray=True, clip=None):
        """One frame.

        `region` is (x, y, w, h) in CAPTURED PIXELS and crops after decoding.
        `clip`   is (x, y, w, h) in CSS PIXELS and crops server-side, so only
                 that area is encoded and transferred — cheaper on a hot polling
                 loop, where full-frame capture costs ~82 ms. Note that a clipped
                 frame's pixel origin is the clip origin, so coordinates from it
                 are NOT in full-frame space; offset them back yourself if you
                 need to click what you found.
        """
        png = self.cdp.screenshot(clip=clip)
        buf = np.frombuffer(png, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)          # BGR
        if img is None:
            raise RuntimeError("failed to decode screenshot PNG")
        if region:
            x, y, w, h = region
            img = img[y:y + h, x:x + w]
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if gray else img

    def to_click_coords(self, px, py):
        """Captured-pixel point -> CSS coordinates for Input.dispatchMouseEvent."""
        return px / self.dpr, py / self.dpr
