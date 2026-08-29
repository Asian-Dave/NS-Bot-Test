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
        # Optional "the bot is doing something" callback. EVERY part of the bot
        # captures frames - the resume ladder, farm navigation, gates, missions,
        # minigames - so this is the one hook that covers all of them. The panel
        # uses it to know it has not been abandoned; hooking the gate alone was
        # not enough, because the farm's own navigation never enters a gate and
        # the panel went stale for the whole of it.
        self.on_activity = None
        self.dpr = float(cdp.evaluate("window.devicePixelRatio") or 1)
        vp = cdp.evaluate("JSON.stringify({w: innerWidth, h: innerHeight})")
        import json
        vp = json.loads(vp)
        self.viewport = (vp["w"], vp["h"])

    def frame(self, region=None, gray=True, clip=None, scale=None):
        """One frame.

        `region` is (x, y, w, h) in CAPTURED PIXELS and crops after decoding.
        `clip`   is (x, y, w, h) in CSS PIXELS and crops server-side, so only
                 that area is encoded and transferred — cheaper on a hot polling
                 loop, where full-frame capture costs ~82 ms. Note that a clipped
                 frame's pixel origin is the clip origin, so coordinates from it
                 are NOT in full-frame space; offset them back yourself if you
                 need to click what you found.
        `scale`  MULTIPLIES the device pixel ratio, it does not replace it. A
                 clip already comes back at dpr, so scale=1 (the default) puts a
                 clipped frame in the SAME pixel space as a full frame. Measured
                 on this host at dpr 2: a 600x452 CSS clip returns 1200x904 at
                 scale 1 and 2400x1808 at scale 2. Use `clip_for` and leave this
                 alone.
        """
        if self.on_activity is not None:
            try:
                self.on_activity()
            except Exception:
                pass
        png = self.cdp.screenshot(clip=clip,
                                  scale=(1.0 if scale is None else scale))
        buf = np.frombuffer(png, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)          # BGR
        if img is None:
            raise RuntimeError("failed to decode screenshot PNG")
        if region:
            x, y, w, h = region
            img = img[y:y + h, x:x + w]
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if gray else img

    def clip_for(self, x, y, w, h):
        """CAPTURED-pixel box -> the (clip, origin) pair `frame` needs.

        Returns the CSS-pixel clip to pass to `frame(clip=...)` and the
        captured-pixel origin of the resulting image, so a point found in the
        clip maps back with `full = clipped + origin`.

        TWO CORRECTIONS, BOTH MEASURED, BOTH SILENT IF YOU GET THEM WRONG
        -----------------------------------------------------------------
        1. **A clip is DOCUMENT-relative; a full frame is the VIEWPORT.** The
           game page sits at scrollY=301, so a clip computed straight from
           viewport pixels lands 602 captured px too high and reads the wrong
           part of the screen. Adding the scroll offset takes the difference
           against the same region of a full frame from a mean of 76.50 to
           EXACTLY 0.00.
        2. **`scale` multiplies the device pixel ratio rather than replacing
           it.** The clip is already at dpr, so the correct scale is 1.

        Both were found the same way - by cropping the identical box out of a
        full frame and differencing - which is the only way to be sure, because
        a mis-clipped frame still decodes, still has plausible dimensions, and
        still reads confident nonsense out of every cell.

        The scroll position is read live because the page can be scrolled between
        calls. The extra round trip is a few ms against a ~42 ms capture.
        """
        import json
        try:
            s = json.loads(self.cdp.evaluate(
                "JSON.stringify({x:scrollX,y:scrollY})") or "{}")
            sx, sy = float(s.get("x", 0)), float(s.get("y", 0))
        except Exception:
            sx = sy = 0.0
        clip = (x / self.dpr + sx, y / self.dpr + sy,
                w / self.dpr, h / self.dpr)
        return clip, (x, y)

    def scroll_game(self, frac=0.0):
        """Park the page scroll so a known part of the game is on screen.

        THE GAME DOES NOT FIT THE VIEWPORT. Measured: the /play iframe is 839
        CSS px tall against a 720 px viewport, so **119 px is always hidden**,
        and which 119 depends on where the page happens to be scrolled. At
        scrollY=458 the top of the game sat 157 px above the viewport and the
        Special tab was not on screen at all - the bot reported "could not find
        the Special tab" while looking at a perfectly healthy Mission Room, and
        the resume ladder halted on a screen it knows, because that screen's
        anchor was scrolled out of view.

        Raising the viewport is NOT the fix: Ruffle scales by
        min(vw/960, vh/720), so a taller viewport rescales the whole game and
        invalidates every template threshold in the project. The scroll is what
        has to be pinned instead.

        `frac` 0.0 puts the top of the game at the top of the viewport, 1.0 the
        bottom. Anything that might sit in the hidden band must be looked for at
        both.
        """
        js = """(() => {
          const f = document.querySelector('iframe[src*="emulator"]')
                 || document.querySelector('iframe[src*="play"]');
          if (!f) return -1;
          const r = f.getBoundingClientRect();
          const top = r.y + scrollY;
          const over = Math.max(0, r.height - innerHeight);
          scrollTo(0, Math.round(top + over * %f));
          return scrollY;
        })()""" % float(frac)
        try:
            return self.cdp.evaluate(js)
        except Exception:
            return None

    def to_click_coords(self, px, py):
        """Captured-pixel point -> CSS coordinates for Input.dispatchMouseEvent."""
        return px / self.dpr, py / self.dpr
