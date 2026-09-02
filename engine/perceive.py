"""Perception layer.

Two complementary mechanisms, because neither alone is enough:

  * Template matching  - "is this button on screen, and where?"
  * Colour sampling    - "how full is this bar / is this icon greyed out?"

Thresholds are the fiddly part, so read this before tuning:

  TM_CCOEFF_NORMED returns roughly -1..1 and is invariant to uniform brightness
  shifts, which is why it is preferred over TM_SQDIFF here. In practice:

      > 0.99   the template was cut from this exact frame (self-match)
      0.90+    same asset, same scale, clean background   -> safe
      0.80-0.90 same asset with some background variation -> usable, verify
      < 0.80   treat as absent; below this false positives appear quickly

  It is NOT scale invariant. Measured on this game, the popup close buttons come
  in two size classes (~59px and ~136px discs), so either keep one template per
  class or pass `scales` to search a range. Searching scales costs time linearly,
  so keep the list short.

  Semi-transparent art makes bad templates. The village labels ("Hunting House",
  "Battle") are drawn over animated scenery, so the background bleeds through and
  changes frame to frame. Prefer opaque assets like the bottom-bar icons.
"""
import os
import weakref

import cv2
import numpy as np

# Calibrated masks. These are MEASURED, not guessed - see CLAUDE.md. A looser red
# range (accepting R>=90) also matches the dark empty portion of a bar's track and
# makes every bar read as 100% full, which produced three wrong diagnoses in a row.
HP_FILL_BGR = ((0, 0, 140), (70, 70, 255))
CLAIMED_TICK_BGR = ((40, 190, 110), (130, 255, 215))
CURRENT_DAY_TAB_BGR = ((0, 0, 140), (80, 85, 255))


# ---------------------------------------------------------------------------
# SPEED. `matchTemplate` cost is linear in frame AREA, and this is the hottest
# path in the whole bot - every subsystem goes through it.
#
# Measured on a 3440x1440 capture: ONE template at ONE scale cost 73 ms, so
# scoring all 59 cost 4.53 s and a single resume-ladder step cost ~1.7 s. Two
# independent fixes compound to about 7x, and neither loses a real match.
# ---------------------------------------------------------------------------

# 1. SEARCH ONLY THE GAME. The game occupies 56% of the capture width; the rest
#    is desktop wallpaper and the bot's own panel. Cropping to it measured
#    1.95x, and it does not merely preserve accuracy - it IMPROVES it, because
#    there is less unrelated art to match by accident:
#
#        lobby_rail_fortune (positive)  0.976 -> 0.976   identical
#        char_slot_level    (negative)  0.512 -> 0.443   MORE margin
#        page_next          (negative)  0.548 -> 0.531   MORE margin
#
#    The band is set from the LIVE game rect every cycle, never hardcoded - the
#    game drifts, and a band remembering where the game used to be would lose
#    every anchor at once. If it cannot be measured the band is cleared and the
#    full frame is searched: slower, but never wrong. Same principle as
#    `Capture.game_offset` returning (0, 0) rather than guessing.
#
#    Coordinates OUT are always full-frame. The crop is an internal detail and
#    the match centre is translated back, because this project has already been
#    burned by a half-applied coordinate correction ("one coordinate space, or
#    none") - the memory board read corrected origins and clicked raw ones.
_BAND = None


def set_search_band(x0, x1):
    """Restrict template search to captured-x [x0, x1). Call every cycle."""
    global _BAND
    _BAND = (int(x0), int(x1)) if x1 > x0 else None


def clear_search_band():
    global _BAND
    _BAND = None


def get_search_band():
    return _BAND


# 2. REJECT NEGATIVES AT HALF RESOLUTION. A negative costs exactly as much as a
#    positive, and almost everything scored is a negative - "no anchor matched"
#    is the common case and the expensive one. So score at half scale first and
#    only pay full price for candidates that could still clear the threshold.
#
#    Halving squeezes the margin from BOTH ends - positives fall, negatives rise
#    - so the cheap gate sits RELAX below the real threshold. The value is
#    measured, not chosen: across 26 committed frames x 59 templates,
#
#        relax  missed positives  worst headroom  negatives rejected
#        0.10          1              -0.007            98.6%
#        0.18          0              +0.073            96.1%
#        0.30          0              +0.193            90.2%
#
#    0.10 already loses a real match. 0.18 is the smallest value with zero
#    misses AND headroom above the 0.07 that thresholds are calibrated to
#    (peak - 0.07). The tightest positive is `nav_jutsu`.
#
#    NOTE FOR MASKED TEMPLATES: none of the current 59 carry alpha, but
#    SWF-extracted assets do, and they score with TM_CCORR_NORMED, whose values
#    are not comparable to TM_CCOEFF_NORMED. RELAX was verified only on the
#    unmasked set, so re-measure this table before trusting the prefilter on a
#    masked template. Until then masked templates skip the prefilter entirely.
COARSE_RELAX = 0.18
COARSE_SCALE = 0.5
# Below this a halved template has too few pixels to discriminate, and the
# prefilter would be both unreliable and pointless.
COARSE_MIN_SIDE = 16
# Halving a small frame saves less than the resize costs.
COARSE_MIN_AREA = 400_000

_half_frame = (None, None)      # (weakref to the frame, its halved copy)


def _halved(frame_gray):
    """A half-scale copy of this frame, reused across templates.

    Keyed on the frame OBJECT, so one sweep over 59 templates resizes once
    instead of 59 times. A weakref rather than `id()`: an id can be recycled by
    a later array once the original is freed, which would silently hand back
    somebody else's pixels. Assumes captures are not mutated in place - they
    are freshly decoded each frame.
    """
    global _half_frame
    ref, cached = _half_frame
    if ref is not None and ref() is frame_gray:
        return cached
    small = cv2.resize(frame_gray, None, fx=COARSE_SCALE, fy=COARSE_SCALE,
                       interpolation=cv2.INTER_AREA)
    try:
        _half_frame = (weakref.ref(frame_gray), small)
    except TypeError:            # not weak-referenceable; just do not cache
        _half_frame = (None, None)
    return small


class Template:
    """One matchable asset plus its tuned threshold."""

    def __init__(self, name, path, threshold=0.88, scales=None):
        self.name = name
        self.path = path
        self.threshold = threshold
        self.scales = scales or [1.0]
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise FileNotFoundError(f"template not readable: {path}")

        # ALPHA IS KEPT when present, and it matters.
        #
        # Templates cut from screenshots are opaque, so this used to just drop
        # the channel. But templates extracted from the game's own SWF
        # (ref/swf_assets/, pulled out of ninja_saga.swf) carry real
        # transparency, and compositing them onto a guessed background is
        # exactly wrong: the pixels behind a UI element differ per screen.
        #
        # Measured on the green check: 0.797 composited onto white, 0.929 with a
        # mask. The red X went 0.769 -> 0.971. So a mask is not a refinement, it
        # is the difference between usable and not.
        self.alpha = None
        if img.ndim == 3 and img.shape[2] == 4:
            a = img[:, :, 3]
            if a.min() < 250:                  # genuinely transparent somewhere
                self.alpha = a
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        self.gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        self.h, self.w = self.gray.shape[:2]
        self._halves = None          # lazily built half-scale twins

    @property
    def masked(self):
        return self.alpha is not None

    def halved(self, s=1.0):
        """This template at `s` * COARSE_SCALE, cached per scale.

        Returns (gray, alpha) or None when the result would be too small to
        discriminate - a 12x8 patch matches almost anything.
        """
        if self._halves is None:
            self._halves = {}
        if s in self._halves:
            return self._halves[s]
        f = s * COARSE_SCALE
        w, h = max(1, int(round(self.w * f))), max(1, int(round(self.h * f)))
        out = None
        if min(w, h) >= COARSE_MIN_SIDE:
            g = cv2.resize(self.gray, (w, h), interpolation=cv2.INTER_AREA)
            a = (cv2.resize(self.alpha, (w, h), interpolation=cv2.INTER_AREA)
                 if self.alpha is not None else None)
            out = (g, a)
        self._halves[s] = out
        return out


class Match:
    def __init__(self, name, confidence, center, scale, size):
        self.name, self.confidence = name, confidence
        self.center, self.scale, self.size = center, scale, size

    @property
    def found(self):
        return self.confidence > 0

    def __repr__(self):
        return (f"<Match {self.name} conf={self.confidence:.3f} "
                f"at={self.center} scale={self.scale}>")


def _score(img, t, a):
    """Peak correlation of one template against one image, plus its location."""
    if t.shape[0] > img.shape[0] or t.shape[1] > img.shape[1]:
        return -1.0, (0, 0)
    if a is None:
        res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
    else:
        res = cv2.matchTemplate(img, t, cv2.TM_CCORR_NORMED, mask=a)
        # A mask can produce NaN/inf where the window is degenerate.
        res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
    _, mx, _, mloc = cv2.minMaxLoc(res)
    return float(mx), mloc


def _coarse_reject(frame_gray, tpl):
    """True when half-resolution says this template cannot possibly clear its
    threshold, so the full-price match can be skipped entirely.

    Conservative by construction: it only ever says "definitely not", and the
    gate sits COARSE_RELAX below the real threshold so a positive that merely
    loses confidence to halving still survives.
    """
    if tpl.masked:                      # not calibrated for TM_CCORR_NORMED
        return False, -1.0
    if frame_gray.shape[0] * frame_gray.shape[1] < COARSE_MIN_AREA:
        return False, -1.0
    small = None
    best = -1.0
    for s in tpl.scales:
        hv = tpl.halved(s)
        if hv is None:                  # too small to judge cheaply
            return False, -1.0
        if small is None:
            small = _halved(frame_gray)
        mx, _ = _score(small, hv[0], hv[1])
        if mx > best:
            best = mx
    return best < (tpl.threshold - COARSE_RELAX), best


def find(frame_gray, tpl: Template, coarse=True):
    """Best single match for `tpl`. Returns a Match with confidence 0 if under
    threshold, so callers can log the near-miss score instead of a bare False.

    A template carrying alpha is matched with a MASK, so transparent pixels are
    ignored rather than compared against whatever happens to be behind the
    element. That path uses TM_CCORR_NORMED because it is the correlation method
    OpenCV supports masking for; scores from the two methods are not directly
    comparable, so a masked template needs its own calibrated threshold.
    """
    # SEARCH ONLY THE GAME, and remember what was cut off so the coordinates
    # handed back are still full-frame ones.
    dx = 0
    band = _BAND
    if band is not None and frame_gray.shape[1] >= band[1] > band[0] >= 0:
        # PAD BY THE TEMPLATE'S OWN WIDTH. `matchTemplate` requires the template
        # to fit ENTIRELY inside the searched region, so a match that straddles
        # the band edge is not degraded - it is annihilated.
        #
        # Measured: `lobby_logo` is 216 px wide and matches with its centre at
        # x=862, which is inside a band starting at 760 - but its LEFT EDGE sits
        # at 754, six pixels outside. Cropping to the bare band took it from
        # 0.998 to 0.299 and the lobby stopped being recognised. Twenty-one such
        # mismatches showed up across the committed frames.
        #
        # Padding by the full width (rather than half) is deliberately generous:
        # the median template is 182 px, so a typical search still covers only
        # ~2284 px of 3440 instead of the bare 1920. Correctness first, and the
        # saving is still most of what the bare band offered.
        x0 = max(0, band[0] - tpl.w)
        x1 = min(frame_gray.shape[1], band[1] + tpl.w)
        if x1 - x0 < frame_gray.shape[1]:
            frame_gray = frame_gray[:, x0:x1]
            dx = x0

    if coarse:
        skip, cheap = _coarse_reject(frame_gray, tpl)
        if skip:
            # The reported confidence is the HALF-RESOLUTION estimate here, not
            # a full-price measurement. It is only ever a near-miss diagnostic -
            # the value is by definition below threshold - but do not calibrate
            # a threshold against a number that came from this path.
            return Match(tpl.name, 0.0, None, 1.0, None), cheap

    best = (-1.0, None, 1.0, None)
    for s in tpl.scales:
        t = tpl.gray
        a = tpl.alpha
        if s != 1.0:
            interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
            t = cv2.resize(t, (max(1, int(tpl.w * s)), max(1, int(tpl.h * s))),
                           interpolation=interp)
            if a is not None:
                a = cv2.resize(a, (t.shape[1], t.shape[0]), interpolation=interp)
        mx, mloc = _score(frame_gray, t, a)
        if mx > best[0]:
            th, tw = t.shape[:2]
            best = (mx, (mloc[0] + tw // 2 + dx, mloc[1] + th // 2), s, (tw, th))
    conf, center, scale, size = best
    # Report the observed confidence even on failure - that number is what you
    # tune against. Zero it only for the caller's found/not-found decision.
    return Match(tpl.name, conf if conf >= tpl.threshold else 0.0,
                 center, scale, size), conf


def find_character(frame_bgr, x0=760, x1=2680, y0=400, y1=950,
                   sat=150, min_area=600, max_area=12000, min_h=95,
                   max_aspect=0.95):
    """Our own character, by SATURATION rather than hue. (x, y) or None.

    Hue is the wrong invariant - gear changes. At Lv 65 this character wears
    purple, and a red-robe search returns None, which is how the Kekkai seal
    hunt lost its heading: `heading_from_spawn` then defaulted to "right" and
    ran the character back through the edge it had just entered by, repeatedly.

    What does not change is that a player sprite is far more saturated than the
    painted scenery, and is small and TALL. Measured with the map band isolated:

        desert sand           saturation median 111, p90 126
        character (purple)    area 1245, bbox  62x107
        character (other map) area 1291, bbox  62x106
        character (third map) area 1585, bbox  79x123

    THE Y BAND MATTERS AT BOTH ENDS. A character stands on GROUND, and the
    scenery above it is saturated too. Measured across every committed frame,
    real characters sit at y 487..805, while live mis-picks came in at y 237 and
    292 - up in the rooftops, where traversal then clicked (800, 292) instead of
    on the path. The default 400 floor sits between them with ~90 px of margin
    either side. Callers should NOT override it with a wider band: the Kekkai
    runner passed (200, 1150) and got a rooftop back on a frame where mission
    traversal correctly returned None.

    TALLEST WINS, NOT LARGEST: a saturated shrub measured 48x77 / area 2534 and
    beat the real character's 79x123 / area 1585 on area alone. Heights are
    106, 107 and 123 against the shrub's 77, so `min_h` 95 sits in the gap.

    This is shared by mission traversal and the Kekkai runner deliberately -
    they had two different finders and only one of them was fixed.
    """
    h, w = frame_bgr.shape[:2]
    x0, x1 = max(0, min(x0, w)), max(0, min(x1, w))
    y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    hsv = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    m = (((hsv[:, :, 1] > sat) & (hsv[:, :, 2] > 60)).astype(np.uint8) * 255)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, _, st, ce = cv2.connectedComponentsWithStats(m)
    best = None
    for i in range(1, n):
        a = st[i, cv2.CC_STAT_AREA]
        bw, bh = st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
        if not (min_area <= a <= max_area) or bh < min_h:
            continue
        if bw / max(1, bh) > max_aspect:
            continue
        if best is None or bh > best[0]:
            best = (bh, int(ce[i][0]) + x0, int(ce[i][1]) + y0)
    return (best[1], best[2]) if best else None


def mask_stats(frame_bgr, lo, hi):
    """Count and locate pixels inside an inclusive BGR range.

    Used instead of template matching where a colour is the signal. Measured
    example: the daily-reward claimed ticks segment on bright yellow-green and
    returned 3318 and 3323 px for the two claimed days - near identical, which
    makes 'count the green blobs' a sturdier success check than matching a tick
    template. Also useful for deriving *which* day is current from the red tab's
    x-centroid, since the tab art contains a day-specific glyph.
    """
    m = cv2.inRange(frame_bgr, np.array(lo, np.uint8), np.array(hi, np.uint8))
    n = int(m.sum() // 255)
    if n == 0:
        return {"pixels": 0, "centroid": None, "bbox": None}
    ys, xs = np.nonzero(m)
    return {"pixels": n,
            "centroid": (int(xs.mean()), int(ys.mean())),
            "bbox": (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))}


def bar_fill_ratio(frame_bgr, x, y, w, lo, hi):
    """How full is a horizontal bar, 0..1.

    Reads a 1px scanline and finds how far the fill colour extends. This is the
    right way to read HP/CP: the values are rasterised text that would need OCR,
    but the bar itself is a pure geometric measurement.
    """
    row = frame_bgr[y:y + 1, x:x + w]
    m = cv2.inRange(row, np.array(lo, np.uint8), np.array(hi, np.uint8))[0]
    filled = np.nonzero(m)[0]
    return 0.0 if len(filled) == 0 else float(filled.max() + 1) / w


def is_desaturated(frame_bgr, x, y, w, h, sat_threshold=40):
    """True if a region looks greyed out — the usual 'on cooldown' tell.

    A ready skill icon is saturated colour; a cooling one is rendered grey. Mean
    HSV saturation separates them far more reliably than matching two templates.
    """
    patch = frame_bgr[y:y + h, x:x + w]
    if patch.size == 0:
        return None
    sat = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)[:, :, 1]
    return float(sat.mean()) < sat_threshold, float(sat.mean())

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Template LOADING lives with template MATCHING. It used to sit in `bot.py`, a
# dry-run observation loop that also happened to hold the only function the
# real bot imported from it - 389 lines carried for one 25-line helper, and a
# second `__main__` that could attach to the same game as the live bot.

def load_templates(cfg, log):
    """Load every template the config names, skipping any that are absent."""
    out, missing = {}, []
    for name, spec in cfg.get("templates", {}).items():
        if name.startswith("_"):
            continue
        path = os.path.join(ROOT, spec["path"])
        if not os.path.exists(path):
            missing.append(name)
            continue
        out[name] = Template(name, path, threshold=spec.get("threshold", 0.88))
    # anything in tpl/ that the config forgot is still worth scoring
    tpl_dir = os.path.join(ROOT, "tpl")
    for f in sorted(os.listdir(tpl_dir)):
        if not f.endswith(".png") or f.startswith("_"):
            continue
        n = f[:-4]
        if n not in out:
            out[n] = Template(n, os.path.join(tpl_dir, f), threshold=0.88)
    if missing:
        log.warning("config names %d template(s) with no file: %s",
                    len(missing), ", ".join(missing))
    log.info("loaded %d templates", len(out))
    return out
