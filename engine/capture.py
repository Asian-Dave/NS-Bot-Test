"""Capture layer — page pixels via CDP, decoded for OpenCV.

Not mss. We never capture a screen, which is why the host needs no Screen
Recording permission and why the game window does not have to be frontmost.
Page.captureScreenshot composites the Ruffle WebGL canvas directly.

Coordinate note: inside the container devicePixelRatio is 1, so captured pixels
and CSS click coordinates are 1:1. On a Retina host it is 2, and captured pixels
are twice the click coordinates. `self.dpr` records which world we are in so
callers never have to guess.
"""
import time

import cv2
import numpy as np

# A LEAF MODULE, so this cannot cycle: `perceive` imports nothing internal.
import perceive


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
        self._off = (0, 0, 1.0)
        self._off_at = 0.0
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
        # Keep the template matcher aimed at the live game rect. Only for
        # FULL frames: a clipped or region frame has its own pixel origin, so a
        # full-frame band would be meaningless there (and `find` additionally
        # refuses to band a frame narrower than the band itself).
        if clip is None and region is None:
            try:
                self.apply_search_band()
            except Exception:
                perceive.clear_search_band()

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
        # NORMALISE FULL FRAMES ONLY. A clipped or region frame has its own
        # pixel origin and its caller already offsets it back by hand; moving
        # it here would shift it twice. See the note beside `norm_shift`.
        if clip is None and region is None:
            try:
                dx, dy = self.measure_shift()
                img = self._translate(img, dx, dy)
                # RECORD WHAT WAS APPLIED. Everything that converts a
                # coordinate back - clicks, clips, the no-click zone - reads
                # this rather than re-measuring, so the round trip closes
                # even while the layout is in flux. See `norm_shift`.
                self._applied_shift = (dx, dy)
            except Exception:
                pass                       # an unmeasurable game must not
                                           # break capture; leave it be
        if region:
            x, y, w, h = region
            img = img[y:y + h, x:x + w]
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if gray else img

    # ---- where the game actually is -------------------------------------
    #
    # EVERY absolute coordinate in this project was measured with the game
    # canvas at one place. When the game moves, they all miss at once - and each
    # subsystem then reports a fault in ITSELF, never the real cause: the memory
    # board went "board gone", the kekkai runes became un-clickable, the Special
    # tab "could not be found". Measured during one such episode: scrollY 60 and
    # the iframe at y = -118 CSS = -236 captured px, matching the -237 by which
    # the card grid had apparently moved.
    #
    # `Runner.ensure_focus` re-aligns every cycle so this SHOULD stay (0, 0).
    # This is the safety net for when it cannot - focus mode off, an unexpected
    # reflow - so a displaced game degrades into slightly-off clicks rather than
    # a cascade of subsystems each blaming itself.
    REFERENCE_ORIGIN = (760, 0)          # captured px, where constants were cut
    REFERENCE_CANVAS_W = 1920            # the game is 960 CSS wide at dpr 2

    def game_metrics(self, ttl=1.0):
        """Where the game is and how big, in CAPTURED px. (ox, oy, scale).

        MEASURED, so the two axes are not guessed:

          * changing the VIEWPORT does not resize the game at all - it stays
            960x839 CSS and merely RE-CENTRES (x went 380 -> 240 -> 480 -> 160
            for viewports 1720/1440/1920/1280). That is pure offset.
          * changing the DPR keeps it 960x839 CSS but scales the captured pixels:
            canvas width 960 / 1920 / 2880 at dpr 1 / 2 / 3. That is pure scale.

        So there is no table of window sizes to learn - the client is one fixed
        960-wide stage (its AIR manifest is `resizable`/`fullScreen` around
        `<width>960</width>`), and every size is a uniform transform of it.
        """
        now = time.time()
        if now - self._off_at < ttl:
            return self._off
        self._off_at = now
        try:
            raw = self.cdp.evaluate(
                "(()=>{const f=document.querySelector('iframe[src*=emulator]')"
                "||document.querySelector('iframe[src*=play]');"
                "if(!f)return '';const r=f.getBoundingClientRect();"
                "return JSON.stringify({x:r.x,y:r.y,w:r.width});})()")
            if not raw:
                # NO GAME ELEMENT. Distinguished from "measured, and it is
                # exactly at the reference" because those return the same
                # numbers and must NOT be treated alike: a consumer that
                # narrows a search to where the game *should* be needs to know
                # the difference between a measurement and a shrug.
                self._off = (0, 0, 1.0)
                self._off_ok = False
                return self._off
            import json as _json
            r = _json.loads(raw)
            ox = int(round(r["x"] * self.dpr)) - self.REFERENCE_ORIGIN[0]
            oy = int(round(r["y"] * self.dpr)) - self.REFERENCE_ORIGIN[1]
            w = r["w"] * self.dpr
            scale = (w / self.REFERENCE_CANVAS_W) if w > 0 else 1.0
            self._off = (ox, oy, scale)
            self._off_ok = True
        except Exception:
            self._off = (0, 0, 1.0)
            self._off_ok = False
        return self._off

    def game_metrics_ok(self):
        """Did the last `game_metrics` actually measure, or fall back?"""
        return bool(getattr(self, "_off_ok", False))

    def apply_search_band(self, ttl=1.0):
        """Point the template matcher at where the game IS, right now.

        `matchTemplate` costs time linear in frame AREA, and the game occupies
        just over half the capture width - the rest is desktop wallpaper and
        the bot's own panel. Restricting the search measured **7.55x** end to
        end (with the half-resolution prefilter), over 6018 template/frame
        comparisons with ZERO changes to any decision or coordinate.

        Called per frame rather than per cycle, because a mission blocks for
        minutes and this project's rule is to ASK THE PAGE rather than
        remember - a band that describes where the game used to be would lose
        every anchor at once. `game_metrics` is TTL-cached, so the real cost is
        at most one CDP round trip per second.

        IF IT CANNOT BE MEASURED THE BAND IS CLEARED, not guessed: searching
        the whole frame is merely slower, while searching the wrong strip is
        wrong. Same rule as `game_offset` returning (0, 0) rather than a guess.
        """
        try:
            ox, _, scale = self.game_metrics(ttl)
            w = self.REFERENCE_CANVAS_W * scale
            if not self.game_metrics_ok() or w <= 0:
                perceive.clear_search_band()
                return None
            # NORMALISED FRAMES PUT THE GAME AT THE REFERENCE by construction,
            # so the band is the reference strip and `ox` must NOT be added -
            # doing so would aim the search at where the game was BEFORE the
            # frame was translated, i.e. at nothing, and lose every anchor at
            # once. That is the failure this band already has a rule about.
            x0 = self.REFERENCE_ORIGIN[0] + (0 if self.normalise else ox)
            band = (max(0, int(round(x0))), int(round(x0 + w)))
            perceive.set_search_band(*band)
            return band
        except Exception:
            perceive.clear_search_band()
            return None

    def game_scale(self, ttl=1.0):
        """How much bigger/smaller the game is than the reference layout."""
        return self.game_metrics(ttl)[2]

    # ---- normalising the frame ------------------------------------------
    #
    # ONE COORDINATE SPACE, OR NONE - and this is the "or none" finally paid
    # off. Every absolute constant in this project was measured with the game
    # at `REFERENCE_ORIGIN`, and `fix` exists to correct for it having moved.
    # But `fix` only helps the callers that REMEMBER to call it: measured,
    # there are 47 hardcoded coordinates across 13 modules and exactly three
    # of them (cards, kekkai_play, mission) correct at all. `click_pixel`
    # deliberately does not correct either, because a template-derived point
    # is already in live coordinates and would be corrected TWICE - which is
    # the half-applied correction that once left the memory board reporting
    # "19 faces known, 11 pairs refused".
    #
    # So instead of asking 47 call sites to agree, move the PICTURE. Every
    # full frame is translated so the game lands exactly where the constants
    # expect it, and the inverse is applied once, at the single point where a
    # coordinate leaves for the browser (`to_click_coords`). After that every
    # constant, template match, detector and test is in one space by
    # construction, and there is nothing left to forget.
    #
    # It SUBSUMES the drift correction rather than competing with it: once
    # frames are normalised the game is always at the reference, so
    # `game_offset` is (0, 0) and `fix` is the identity - which also means the
    # scroll/reflow drift this class was built for is absorbed for EVERY
    # module, not just the three that opted in.
    #
    # Set `normalise = False` to get the historical behaviour back in one
    # step, which is the point of it being a flag.
    normalise = True

    def norm_shift(self, ttl=1.0):
        """(dx, dy) a captured frame must move so the game sits at reference.

        THE INVERSE MUST MATCH THE FORWARD TRANSFORM, NOT A LATER READING.
        This returns the shift `frame` ACTUALLY APPLIED, because a coordinate
        being converted back was derived from a frame that was translated by
        that amount - re-measuring can disagree with it, and then the round
        trip does not close.

        Measured, and it wedged a run: during a relog the game is briefly
        unmeasurable, a fresh read returned (0, 0), and a reference-space
        point was compared against a real-space no-click zone -

            REFUSING click (2555,248) close Eudemon Garden - it lands on the
            control dock (1920, 0, 760, 1800)

        - though 2555 - 760 = 1795 is well clear of it. Pinning the applied
        value makes that impossible by construction rather than by hoping the
        two measurements agree.

        Before any frame has been taken there is nothing to match, so it
        measures; and a missing measurement still yields (0, 0) rather than a
        guess, the rule `game_offset` already follows.
        """
        if not self.normalise:
            return (0, 0)
        applied = getattr(self, "_applied_shift", None)
        if applied is not None:
            return applied
        return self.measure_shift(ttl)

    def measure_shift(self, ttl=1.0):
        """The shift the CURRENT layout calls for. Used by `frame`."""
        if not self.normalise:
            return (0, 0)
        ox, oy, _sc = self.game_metrics(ttl)
        if not self.game_metrics_ok():
            # Nothing measurable. Keep whatever the last frame used rather
            # than snapping to zero: the coordinates in flight came from that
            # frame, and disagreeing with them is worse than being slightly
            # stale. With no history at all, (0, 0) is the honest answer.
            return getattr(self, "_applied_shift", None) or (0, 0)
        return (-int(ox), -int(oy))

    @staticmethod
    def _translate(img, dx, dy):
        """Shift an image by (dx, dy), growing the canvas rather than cropping.

        Growing matters: cropping to the original size would push the dock off
        the right edge, and the no-click zone is derived from where the dock
        IS. Only positive shifts grow; a negative one crops, which is correct
        because that content is off-screen anyway.
        """
        if not dx and not dy:
            return img
        h, w = img.shape[:2]
        top, left = max(0, dy), max(0, dx)
        out = cv2.copyMakeBorder(img, top, 0, left, 0,
                                 cv2.BORDER_CONSTANT, value=(0, 0, 0))
        if dx < 0 or dy < 0:
            out = out[max(0, -dy):, max(0, -dx):]
        return out

    def game_offset(self, ttl=1.0):
        """How far the game has moved from the reference layout, captured px.

        Cached for `ttl` seconds: it is one CDP round trip and callers may ask
        per click. Returns (0, 0) if the game cannot be located, because a
        missing measurement must never move a click.

        ZERO WHILE NORMALISING, because the frame has already been moved so
        the game IS at the reference - and a caller that corrected again would
        double-correct, which is the exact bug this class documents.
        """
        if self.normalise:
            return (0, 0)
        return self.game_metrics(ttl)[:2]

    def fix(self, x, y):
        """Map a reference-layout coordinate onto the live one.

        Scale about the game's own ORIGIN, then translate - scaling about the
        frame origin instead would smear the offset by the scale factor.
        """
        if self.normalise:
            # The frame was moved instead, so a reference coordinate already
            # IS the live one. Correcting here would apply the shift twice.
            return (int(x), int(y))
        ox, oy, sc = self.game_metrics()
        rx, ry = self.REFERENCE_ORIGIN
        return (int(round((x - rx) * sc)) + rx + ox,
                int(round((y - ry) * sc)) + ry + oy)

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
        # UN-NORMALISE THE BOX, KEEP THE ORIGIN NORMALISED.
        #
        # The caller hands a box in REFERENCE space (that is where all its
        # constants live) but the clip is a real page rectangle, so it needs
        # the same inverse `to_click_coords` applies. The returned ORIGIN does
        # not: it exists so a point found inside the clip maps back with
        # `full = clipped + origin`, and `full` is consumed as a reference
        # coordinate.
        #
        # Missing this is what broke the hand-seal board: `panel_frame` clips
        # around the "Skill :" HUD, the clip was taken 760 px from where the
        # HUD actually is, `anchor_offset` found nothing, and the round was
        # abandoned with "cannot locate the panel" - while the classifier,
        # which uses a FULL frame, had just matched that same HUD at 0.998.
        # Two capture paths, one taught the new coordinate space and the
        # other not.
        dx, dy = self.norm_shift()
        clip = ((x - dx) / self.dpr + sx, (y - dy) / self.dpr + sy,
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
        # DO NOTHING IN FOCUS MODE. Focus mode hides the game's siblings so the
        # document is no taller than the game and the scroll is deterministic -
        # that is its entire purpose. Scrolling then is not just pointless, it
        # MOVES THE GAME UNDER THE BOT and fights the re-align: measured in one
        # session, 40 ladder scrolls against 5 re-aligns, with focus reporting
        # ON while the game sat 58 CSS px above the viewport. From outside that
        # looks like "it scrolls the window down instead of clicking, then tries
        # to realign again".
        js = """(() => {
          if (window.__nsbotFocusOn) return -2;   // pinned; scrolling is harmful
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
        """Captured-pixel point -> CSS coordinates for Input.dispatchMouseEvent.

        THE ONE PLACE THE NORMALISATION IS UNDONE. Everything upstream works
        in the reference space the frame was translated into; the browser
        wants real page coordinates, and this is the single door a coordinate
        leaves by, which is exactly why the inverse belongs here and nowhere
        else. See the note beside `norm_shift`.
        """
        dx, dy = self.norm_shift()
        return (px - dx) / self.dpr, (py - dy) / self.dpr
