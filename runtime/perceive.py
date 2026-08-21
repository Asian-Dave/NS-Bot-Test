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
import cv2
import numpy as np


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
        # Drop alpha before graying; our crops are opaque but be defensive.
        if img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        self.gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        self.h, self.w = self.gray.shape[:2]


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


def find(frame_gray, tpl: Template):
    """Best single match for `tpl`. Returns a Match with confidence 0 if under
    threshold, so callers can log the near-miss score instead of a bare False."""
    best = (-1.0, None, 1.0, None)
    for s in tpl.scales:
        t = tpl.gray
        if s != 1.0:
            t = cv2.resize(t, (max(1, int(tpl.w * s)), max(1, int(tpl.h * s))),
                           interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
        # Template must fit inside the frame or matchTemplate throws.
        if t.shape[0] > frame_gray.shape[0] or t.shape[1] > frame_gray.shape[1]:
            continue
        res = cv2.matchTemplate(frame_gray, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, mloc = cv2.minMaxLoc(res)
        if mx > best[0]:
            th, tw = t.shape[:2]
            best = (mx, (mloc[0] + tw // 2, mloc[1] + th // 2), s, (tw, th))
    conf, center, scale, size = best
    # Report the observed confidence even on failure - that number is what you
    # tune against. Zero it only for the caller's found/not-found decision.
    return Match(tpl.name, conf if conf >= tpl.threshold else 0.0,
                 center, scale, size), conf


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
