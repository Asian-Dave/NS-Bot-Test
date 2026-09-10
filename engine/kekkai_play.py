#!/usr/bin/env python3
"""Drive the Kekkai rune puzzle live. Geometry measured on our own client.

Coordinates are in CAPTURED PIXELS at the standard pinned viewport
(1720x720 @ dpr 2, so the game canvas occupies captured x 760..2680). They were
read off ref/auto/tp/kekkai_puzzle.png with a coordinate grid, not guessed.

RUNE ORDER MATCHES THE REFERENCE BOT EXACTLY
--------------------------------------------
On screen, left to right: green spiral, red spiral, blue triangle, black
lightning, yellow flame, white crescent. The reference bot's rune list is
["Green","Red","Blue","Black","Yellow","White"]. Same order, so its indexing
transfers directly and `engine/kekkai.py` can use its names unchanged.

READING THE FEEDBACK — the mapping is now MEASURED
--------------------------------------------------
Each history row shows two stylised digits: a GREEN circle and a GOLD circle.

    GREEN = correct rune in the CORRECT PLACE
    GOLD  = correct rune in the WRONG PLACE

That is not assumed, it was determined by play: both mappings were carried as
live hypotheses and filtered against real feedback until one died. On the first
solved kekkai the history was

    Green,Red,Blue     -> green 0, gold 1
    Red,Black,Yellow   -> green 2, gold 0
    Black,Blue,White   -> green 1, gold 1

which left exactly ONE candidate under each mapping: (Red,Black,White) under
green=correct-place, and (Black,Yellow,Blue) under the inverse. Submitting
(Red,Black,White) produced **"You break the seal!"**, so green=correct-place is
the right reading. `solve_live` still carries both hypotheses, because that costs
nothing and protects against a misread counter.

Digit recognition is by template match against exemplars in
ref/auto/tp/digits/<n>.png. Those do not exist until they have been seen, so
`--dump-row N` saves the two crops for a row and you classify them once; after
that the same crops serve as templates. This is deliberate bootstrapping: the
alternative is inventing digit templates for glyphs we have never observed.
"""
import argparse
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from act import Actor
from capture import Capture
from cdp import CDP, find_page_target
import kekkai
import perceive

# --- measured geometry, captured px ----------------------------------------
RUNE_XY = {
    "Green":  (860, 1076),
    "Red":   (1018, 1076),
    "Blue":  (1166, 1076),
    "Black": (1321, 1076),
    "Yellow": (1486, 1076),
    "White": (1639, 1076),
}
SLOT_XY = {1: (1118, 753), 2: (1260, 753), 3: (1389, 753)}
CLEAR_XY = (1612, 794)

# History rows are LOCATED, not computed from constants.
#
# A fixed y0 + pitch drifted: measured y0 was 290 not 297 and the pitch 88.53 not
# 88.0, which over ten rows is ~25px - enough to crop between two discs and read
# a neighbour's digit. Segmenting the green disc column each frame removes the
# drift entirely, and it also tells us how many rows the scroll has.
HIST_GOLD_DX = 86                  # gold disc sits this far right of the green one
DIGIT_BOX = 34                     # half-width of a digit crop
CONFIRM_XY = (1259, 513)           # kekkai centre; turns dark red when armed


# --- THE PANEL MOVES, SO LOCATE IT ------------------------------------------
#
# The constants above are a REFERENCE LAYOUT, not the truth. Measured live, the
# whole puzzle panel sat ~114-126 px higher than these values:
#
#     rune Green   reference (860, 1076)   actual (880, 964)   delta (+20, -112)
#     rune White   reference (1639, 1076)  actual (1664, 962)  delta (+25, -114)
#     kekkai centre reference (1259, 513)  actual (1261, 387)  delta ( +2, -126)
#
# The rune discs are only r~53, so a 114 px error puts every click clean outside
# the button. The consequence was silent and total: no slot ever filled, so no
# guess was ever submitted, so the history stayed empty - and the solver then
# read row 0, which was an UNPLAYED row, got a dim 0/0 it could not classify,
# and burned the mission. The scroll showed ten identical unplayed rows.
#
# This is the same lesson as the hand-seal board and the battle geometry:
# ANCHOR-RELATIVE, NEVER ABSOLUTE. Here we can do better than an anchor
# template - the features are geometric and can be found directly.

def find_rune_buttons(frame_bgr, y0=820, y1=1220, x0=760, x1=2680):
    """The six rune discs, left to right. [(x, y)] or None.

    They are a row of equally sized circles, which Hough finds directly - no
    template, and it adapts to wherever the panel has been drawn.
    """
    h, w = frame_bgr.shape[:2]
    x0, x1 = max(0, min(x0, w)), max(0, min(x1, w))
    y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    g = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(g, cv2.HOUGH_GRADIENT, dp=1, minDist=60,
                               param1=100, param2=30, minRadius=28,
                               maxRadius=60)
    if circles is None:
        return None
    cs = [(int(c[0]) + x0, int(c[1]) + y0, int(c[2]))
          for c in np.round(circles[0]).astype(int)]

    # THE RUNE ROW HAS TO BE PICKED OUT, NOT ASSUMED TO BE EVERYTHING FOUND.
    # Measured on a live frame: 13 circles in this band - the six rune discs
    # (y~960, r~55), the history scroll's own counter discs (r~39) and a few
    # strays. Requiring "exactly six" therefore just failed and fell back to the
    # reference layout, which is the bug this function exists to fix.
    #
    # Group by y, then find a run of six with consistent spacing AND consistent
    # radius: the rune buttons are a row of identical circles, which nothing
    # else on this screen is.
    want = len(kekkai.RUNES)
    bands = []
    for c in sorted(cs, key=lambda z: z[1]):
        for b in bands:
            if abs(b[0][1] - c[1]) <= 25:
                b.append(c)
                break
        else:
            bands.append([c])
    # AND WHEN THERE ARE TWO SUCH ROWS, PICK THE COLOURED ONE.
    #
    # This used to return the FIRST qualifying run, and bands are sorted by y,
    # so the topmost won. That was right by accident on the TP layout - three
    # numbered slots against six runes, so only the runes ever formed a run of
    # six. The SS puzzle escalates the code length, and at length SIX there are
    # six numbered slots AND six rune discs: two rows of six evenly spaced
    # identical circles. It locked onto the SLOTS, every click landed on an
    # empty numbered slot and did nothing, and six successive guesses read back
    # byte-identical feedback from an untouched board.
    #
    # Saturation does not separate them - measured mean S 75 for the slots
    # against 85 for the runes. HUE SPREAD does, decisively: the rune row is
    # six DIFFERENT colours while the slot row is six of the same parchment
    # tone.
    #
    #     slot row   H = 19 19 19 19 19 19        spread   0
    #     rune row   H = 53 174 120 0 24 15       spread 174
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)

    def _hue_spread(run):
        hs = []
        for x, y, r in run:
            rr = max(6, int(r * 0.6))
            p = hsv[max(0, y - rr):y + rr, max(0, x - rr):x + rr]
            if p.size:
                hs.append(float(np.median(p[:, :, 0])))
        return (max(hs) - min(hs)) if len(hs) > 1 else 0.0

    runs = []
    for band in bands:
        band.sort(key=lambda z: z[0])
        for i in range(len(band) - want + 1):
            run = band[i:i + want]
            radii = [z[2] for z in run]
            if max(radii) - min(radii) > 0.30 * max(radii):
                continue                       # not identical buttons
            gaps = [run[j + 1][0] - run[j][0] for j in range(want - 1)]
            if min(gaps) <= 0:
                continue
            if (max(gaps) - min(gaps)) > 0.30 * max(gaps):
                continue                       # not evenly spaced
            runs.append((_hue_spread(run), run))
    if not runs:
        return None
    runs.sort(key=lambda z: -z[0])
    return [(z[0], z[1]) for z in runs[0][1]]


def find_confirm_point(frame_bgr, y0=200, y1=900, x0=900, x1=1800):
    """The kekkai centre - the disc that SUBMITS when clicked. (x, y) or None.

    A large, round, dark blob inside the puzzle scroll. Measured on a live
    frame: area 32695, bbox 207x201, centre (1261, 387).
    """
    h, w = frame_bgr.shape[:2]
    x0, x1 = max(0, min(x0, w)), max(0, min(x1, w))
    y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    g = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    m = cv2.morphologyEx((g < 90).astype(np.uint8) * 255,
                         cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, _, st, ce = cv2.connectedComponentsWithStats(m)
    best = None
    for i in range(1, n):
        a = st[i, cv2.CC_STAT_AREA]
        bw, bh = st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
        if a < 3000 or bh == 0:
            continue
        if not (0.7 <= bw / bh <= 1.4):        # a disc is round
            continue
        # IT MUST BE A SOLID DISC, NOT A DARK RING.
        #
        # A node's dark circular OUTLINE passes every test above, and its
        # centroid lands in the pale middle of the ring - so this returned a
        # point that was BRIGHT (mean grey 220) out of a mask built from
        # `g < 90`, while the real submit disc is solid and dark (grey 71).
        # On the SS layout that put the submit click on a rune node.
        #
        # Fill separates them the same way it separates a kekkai outline from a
        # solid robe elsewhere in this project: a ring is sparse inside its
        # bounding box, a disc is not.
        if a < 0.55 * bw * bh:
            continue
        # AND NOT "the centre pixel must be dark" - that was tried and it
        # rejected the right blob everywhere. The kanji is drawn LIGHT on the
        # dark disc, so the centroid lands on a light stroke: measured
        # centre-grey 148 on both SS stages and 204 on TP. Fill alone is the
        # discriminator, and it is decisive - the correct disc measures
        # 0.65 / 0.66 / 0.77 across the three layouts while every distractor
        # sits at 0.18..0.47.
        cx, cy = int(ce[i][0]) + x0, int(ce[i][1]) + y0
        if best is None or a > best[0]:
            best = (a, cx, cy)
    return (best[1], best[2]) if best else None


def locate_panel(frame_bgr, log=None, cap=None):
    """Live rune positions and submit point. Falls back to the reference layout.

    Returns (rune_xy: dict, confirm_xy: tuple). Falling back is logged loudly,
    because the reference layout is exactly what silently burned a mission -
    and when we do fall back, `cap.fix` at least corrects it for however far the
    whole GAME has drifted, which was the real cause that day.
    """
    runes = find_rune_buttons(frame_bgr)
    confirm = find_confirm_point(frame_bgr)
    fix = cap.fix if cap is not None else (lambda x, y: (x, y))
    if runes is None:
        if log:
            log.info("could not locate the rune row; falling back to the "
                     "REFERENCE layout corrected for game drift %s",
                     cap.game_offset() if cap is not None else "(unknown)")
        rune_xy = {k: fix(*v) for k, v in RUNE_XY.items()}
    else:
        rune_xy = {name: pt for name, pt in zip(kekkai.RUNES, runes)}
        if log:
            log.info("kekkai runes located: %s", rune_xy["Green"])
    if confirm is None:
        if log:
            log.info("could not locate the kekkai centre; using the reference "
                     "point corrected for game drift")
        confirm = fix(*CONFIRM_XY)
    return rune_xy, confirm


class _Log:
    def info(self, m, *a):
        print(("  " + m) % a if a else "  " + m, flush=True)
    warning = error = info


def find_kekkai(frame, x0=800, x1=2650, y0=200, y1=1150,
                min_area=8000, max_fill=0.45, min_aspect=0.9, max_aspect=2.0,
                min_h=200):
    """Locate an unsealed kekkai in the traversal scene. Returns (x, y) or None.

    Colour alone is NOT enough and getting this wrong wasted a run: a dark-red
    blob search matched our own character's RED ROBE and clicked it, which did
    nothing while the code cheerfully reported success.

    Calibrated against a frame containing both:

        kekkai          area 20622  bbox 481x268  fill 0.160  aspect 1.79
        character robe  area  4040  bbox  65x170  fill 0.366  aspect 0.38

    All three features separate them, so all three are required. The decisive one
    is FILL: a kekkai is a triangle OUTLINE and so sparse inside its bounding
    box, whereas a robe is a solid blob. Aspect helps too - the robe is tall and
    narrow, the kekkai wide.
    """
    fh, fw = frame.shape[:2]
    x0, x1 = max(0, min(x0, fw)), max(0, min(x1, fw))
    y0, y1 = max(0, min(y0, fh)), max(0, min(y1, fh))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    roi = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m = ((((hsv[:, :, 0] < 10) | (hsv[:, :, 0] > 170))
          & (hsv[:, :, 1] > 110) & (hsv[:, :, 2] > 70)).astype(np.uint8) * 255)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    n, _, st, ce = cv2.connectedComponentsWithStats(m)
    best = None
    for i in range(1, n):
        a = st[i, cv2.CC_STAT_AREA]
        if a < min_area:
            continue
        w, h = st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
        if w * h == 0 or h < min_h:
            continue
        ar = w / max(1, h)
        if a / (w * h) > max_fill or not (min_aspect <= ar <= max_aspect):
            continue
        if best is None or a > best[0]:
            best = (a, (x0 + int(ce[i][0]), y0 + int(ce[i][1])))
    return best[1] if best else None


def find_character(frame):
    """Our own character. (x, y) or None.

    DELEGATES to the shared saturation finder. This used to be its own red-robe
    search, which returned None for a purple-robed character - so
    `heading_from_spawn` fell back to "right" and ran the character straight
    back through the edge it had just come in by, over and over. Mission
    traversal had already been fixed; this had not, and the two finders drifting
    apart is exactly how that survived.
    """
    # NOTHING is overridden - band and canvas both come from the shared default.
    # Overriding is how this diverged twice: a y band of (200, 1150) returned a
    # ROOFTOP where mission traversal correctly said None, and an x range of
    # (800, 2650) vs (760, 2680) changed which blobs merge at the edge and so
    # changed the answer again. One caller with different arguments is the same
    # bug as two implementations.
    return perceive.find_character(frame)

def count_nodes(frame, centre, box=(260, 160), min_area=700, max_area=6000):
    """How many pale nodes the seal has — i.e. THE CODE LENGTH.

    The first seal had 3 circular nodes and its code was 3 runes long; the second
    is a 5-node pentagon. So the node count appears to BE the length, which means
    it does not have to be passed in or guessed per mission.

    Nodes are the pale ellipses inside the seal, well separated from the dark-red
    outline and the dark centre disc, so a brightness threshold inside the seal's
    bounding box finds them.
    """
    cx, cy = centre
    x0, x1 = max(0, cx - box[0]), min(frame.shape[1], cx + box[0])
    y0, y1 = max(0, cy - box[1]), min(frame.shape[0], cy + box[1])
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return None
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(g, 185, 255, cv2.THRESH_BINARY)
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(bw)
    return sum(1 for i in range(1, n)
               if min_area <= st[i, cv2.CC_STAT_AREA] <= max_area)


def canvas_x(cap=None):
    """Canvas left/right in CAPTURED px, following the live game.

    CANVAS_X0/X1 are REFERENCE values. Focus mode flush-lefts the game and a
    different viewport moves it, so an edge target computed from the constants
    can land outside the canvas entirely.
    """
    if cap is None:
        return CANVAS_X0, CANVAS_X1
    return cap.fix(CANVAS_X0, 0)[0], cap.fix(CANVAS_X1, 0)[0]


def heading_from_spawn(frame, log=None, cap=None):
    """Which way to run, from where the character is standing.

    You enter a map through one edge, so you spawn NEAR that edge and must head
    AWAY from it. Deriving the heading from the character's position gets this
    right on every map; a fixed or merely-persistent heading does not — with the
    character already at x=2268 against a canvas centre of 1720, a default of
    "right" ran it straight back into the edge it had just come through, over and
    over.

    Returns "right" | "left", defaulting to "right" if the character cannot be
    found (no information is not a reason to stand still).
    """
    cx0, cx1 = canvas_x(cap)
    centre = (cx0 + cx1) // 2
    pos = find_character(frame)
    if pos is None:
        if log:
            log.info("character not located; defaulting heading to right")
        return "right"
    h = "right" if pos[0] < centre else "left"
    if log:
        log.info("character at x=%d (centre %d) -> spawned %s, heading %s",
                 pos[0], centre, "left" if pos[0] < centre else "right", h)
    return h


def read_seals(frame, ex, x0=1300, x1=1900, y0=60, y1=130):
    """The 'Seals: X / Y' HUD. Returns (done, total) or (None, None).

    Knowing the total is what lets the hunt stop for the right reason instead of
    on a step budget.
    """
    # Digits here are white-on-dark rather than the disc glyphs, so reuse of the
    # history exemplars is not safe; report unknown rather than guess.
    return (None, None)


# The history scroll's columns are LOCATED, not assumed - see `find_rows`.
COL_SEARCH = (1880, 2340)     # x range that contains both layouts' scrolls
COL_TOL = 26                  # x spread within one column of discs


def _green_blobs(frame, y0, y1, min_area):
    """Centroids of the green-hue discs across the whole scroll region."""
    h, w = frame.shape[:2]
    x0, x1 = max(0, min(COL_SEARCH[0], w)), max(0, min(COL_SEARCH[1], w))
    y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    roi = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m = (((hsv[:, :, 0] > 35) & (hsv[:, :, 0] < 95)
          & (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 40)).astype(np.uint8) * 255)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, _, stats, cent = cv2.connectedComponentsWithStats(m)
    return [(x0 + cent[i][0], y0 + cent[i][1]) for i in range(1, n)
            if stats[i, cv2.CC_STAT_AREA] >= min_area]


def _columns(pts):
    """Group centroids into columns by x. Returns [(x, [y...]), ...] left first."""
    cols = []
    for x, y in sorted(pts):
        for col in cols:
            if abs(col[0][0] - x) <= COL_TOL:
                col.append((x, y))
                break
        else:
            cols.append([(x, y)])
    out = []
    for col in cols:
        xs = [p[0] for p in col]
        out.append((int(round(sum(xs) / len(xs))),
                    sorted(int(round(p[1])) for p in col)))
    return sorted(out)


def find_rows(frame, x0=None, x1=None, y0=240, y1=1200, min_area=800,
              min_rows=5):
    """Locate the feedback rows by segmenting the GREEN disc column.

    Returns (green_x, [y, ...]) top to bottom, or (None, []) if the panel is not
    open.

    **THE COLUMN'S X IS LOCATED, NOT ASSUMED.** This used to hardcode
    x 1950..2030, measured on the TP kekkai where the green column sits at 1987.
    The SS version of the same puzzle - identical rules, five runes instead of
    three - draws its history scroll further right, so that window contained
    nothing: the minigame reported `history_rows: 0` and classified as
    "unknown". This file already says the scroll "must be LOCATED, not
    computed" about the row Y positions; the same is true of X, and only one
    layout had been seen when that was written.

    **NEITHER A WIDER WINDOW NOR A SWEEP OF NARROW ONES IS THE FIX**, and both
    were tried and measured. One wide window merges the green and gold columns:
    12 "rows" at pitch 71.36 with a standard deviation of 21.16, which is two
    interleaved columns. Sweeping narrow windows and taking each window's
    centroid instead BIASES THE X, because a window that clips a disc puts its
    centroid off-centre - it reported the TP column at 1972 where the true
    value is 1987, and derived a gold offset of 51 against a true 86.

    So segment ONCE over the whole region and cluster the blobs by x. A column
    is then a real group of discs rather than whatever fell inside a chosen
    rectangle, and the centres come out unbiased.
    """
    if x0 is not None and x1 is not None:
        return _find_rows_legacy(frame, x0, x1, y0, y1, min_area, min_rows)
    cols = [c for c in _columns(_green_blobs(frame, y0, y1, min_area))
            if len(c[1]) >= min_rows]
    if not cols:
        return None, []
    # The GREEN column is the leftmost qualifying one; gold sits to its right.
    return cols[0][0], cols[0][1]


# The gold disc needs its OWN hue window, and that is not a detail: it does
# not pass the green filter at all, so looking for a second GREEN column found
# nothing on either layout. Measured at the disc centres -
#
#     gold disc    H p50 = 13 (SS), 22 (TP wgpu), 22 (TP webgl),  V p50 ~ 235
#     green disc   H p50 = 54..57
#
# - so the two separate cleanly on hue, and gold is bright besides.
GOLD_HSV = ((8, 80, 150), (32, 255, 255))


def _gold_blobs(frame, y0, y1, min_area):
    """Centroids of the GOLD discs across the scroll region."""
    h, w = frame.shape[:2]
    x0, x1 = max(0, min(COL_SEARCH[0], w)), max(0, min(COL_SEARCH[1], w))
    y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    roi = frame[y0:y1, x0:x1]
    lo, hi = GOLD_HSV
    m = cv2.inRange(cv2.cvtColor(roi, cv2.COLOR_BGR2HSV),
                    np.array(lo, np.uint8), np.array(hi, np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, _, stats, cent = cv2.connectedComponentsWithStats(m)
    return [(x0 + cent[i][0], y0 + cent[i][1]) for i in range(1, n)
            if stats[i, cv2.CC_STAT_AREA] >= min_area]


def find_gold_dx(frame, green_x, ys, y0=240, y1=1200, min_area=800):
    """How far right of the green column the GOLD one sits. Measured, not fixed.

    `HIST_GOLD_DX` is 86, measured on the TP layout; the SS layout of the same
    puzzle differs, and reading a digit several px off centre is enough to make
    a counter unreadable - which stops the solver dead. Returns None when the
    column cannot be found, so the caller keeps the constant rather than acting
    on a guess.
    """
    for x, cys in _columns(_gold_blobs(frame, y0, y1, min_area)):
        if x <= green_x + COL_TOL:
            continue                        # that is the green column itself
        if abs(len(cys) - len(ys)) <= 2 and abs(cys[0] - ys[0]) <= 14:
            return x - green_x
    return None


def _find_rows_legacy(frame, x0=1950, x1=2030, y0=240, y1=1200, min_area=800,
                      min_rows=5):
    h, w = frame.shape[:2]
    x0, x1 = max(0, min(x0, w)), max(0, min(x1, w))
    y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None, []               # frame smaller than the region of interest
    roi = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m = (((hsv[:, :, 0] > 35) & (hsv[:, :, 0] < 95)
          & (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 40)).astype(np.uint8) * 255)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, _, stats, cent = cv2.connectedComponentsWithStats(m)
    pts = [(x0 + cent[i][0], y0 + cent[i][1]) for i in range(1, n)
           if stats[i, cv2.CC_STAT_AREA] >= min_area]
    if len(pts) < min_rows:
        return None, []
    gx = int(round(sum(p[0] for p in pts) / len(pts)))
    return gx, sorted(int(round(p[1])) for p in pts)


# The rune-icon strip, as an offset from the located GOLD disc.
FILLED_DX = (30, 200)
# A filled row is measured by EDGE DENSITY, not saturation - see count_filled.
FILLED_EDGE = 0.12


def count_filled(frame, x0=None, x1=None, box=28):
    """How many history rows already hold a guess.

    Needed because reading "the row for my Nth guess" as row N-1 is wrong the
    moment the scroll already has entries, and that off-by-one made the solver
    read one guess's feedback off another's row and corrupted its whole model.

    **SATURATION WAS THE WRONG METRIC, and the failure was rune-specific.** The
    old version measured the fraction of strongly-saturated pixels in the rune
    strip. That works until a row contains the BLACK rune, which is dark and
    barely saturated: measured on an SS scroll with eight rows filled, the two
    rows holding black read 0.09 against a 0.12 gate and were counted EMPTY, so
    `count_filled` returned 6 for 8. The solver then read stale rows and only
    converged by luck.
    hue is irrelevant to the question being asked - "is there an icon here, or
    a dash?" - so measure STRUCTURE. Edge density separates cleanly and a black
    rune has just as strong an outline as a bright one:

        filled rows   0.160 .. 0.215   (SS, including the black-rune rows)
                      0.168            (TP)
        empty rows    0.009 .. 0.089   (both layouts, both renderers)

    The window is also RELATIVE to the located gold column rather than
    absolute. It was hardcoded to the TP layout's x 2120..2230; on the SS
    layout the whole scroll sits further right, so that window landed on the
    GOLD DISCS - and a gold disc is saturated whether its row was played or
    not, which returned 10 filled on a scroll with nothing on it at all.
    """
    gx, ys = find_rows(frame)
    if gx is None:
        return 0
    if x0 is None or x1 is None:
        dx = find_gold_dx(frame, gx, ys)
        gold = gx + (dx if dx is not None else HIST_GOLD_DX)
        x0, x1 = gold + FILLED_DX[0], gold + FILLED_DX[1]
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    n = 0
    for y in ys:
        cell = g[max(0, y - box):y + box, max(0, x0):x1]
        if cell.size < 100:
            continue
        e = cv2.Canny(cell, 60, 160)
        if float((e > 0).mean()) >= FILLED_EDGE:
            n += 1
    return n


def row_center(i, frame=None):
    """(green_xy, gold_xy) for history row i, 0-based. Located when given a frame.

    THE GOLD OFFSET IS MEASURED TOO. `HIST_GOLD_DX` is 86, from the TP layout;
    the SS layout of the same puzzle puts the pair 79 px apart. Reading a digit
    7 px off centre is enough to make a counter unreadable, and an unreadable
    counter stops the solver dead.
    """
    if frame is not None:
        gx, ys = find_rows(frame)
        if gx is not None and i < len(ys):
            dx = find_gold_dx(frame, gx, ys)
            if dx is None:
                dx = HIST_GOLD_DX      # measured for TP; better than nothing
            return (gx, ys[i]), (gx + dx, ys[i])
    # fallback to the measured grid if segmentation failed
    y = int(round(290 + i * 88.53))
    return (1987, y), (1987 + HIST_GOLD_DX, y)


def crop_digit(frame, xy):
    x, y = xy
    return frame[y - DIGIT_BOX:y + DIGIT_BOX, x - DIGIT_BOX:x + DIGIT_BOX]


# Where the ink exemplars live. A SEPARATE directory from `digits/`, because
# the two representations are NOT interchangeable and mixing them would be
# silently wrong - see the measurement in `digit_mask`.
INK_DIR = "ref/auto/tp/digits_ink"

# The glyph is a minority of the disc's area. Measured on a live webgl row,
# taking the darkest 30% inside the disc gives mask fractions of 0.150 (green
# "0") and 0.153 (gold "1") - consistent across two very differently coloured
# discs, which is the property the old mask lost.
INK_PCT = 30
INK_DISC_FRAC = 0.40          # of the crop's half-width, so the rim is excluded


def digit_mask(frame, xy, r=26):
    """The counter digit as DARK INK, relative to the disc's own brightness.

    THIS USED TO THRESHOLD BRIGHT PIXELS, and that is why the kekkai stopped
    after guess 1 on webgl. On wgpu the glyph is a dark digit with a WHITE
    OUTLINE, so bright pixels captured the outline and one exemplar set served
    both discs. **webgl draws no outline** - it is the same missing text stroke
    that broke seven templates elsewhere - so the bright mask collapsed:

        green disc (dark)   bright fraction 0.08  - the "0" VANISHED, leaving
                                                    only a specular highlight
        gold disc (light)   bright fraction 0.375 - the DISC went white and the
                                                    "1" became a dark HOLE

    The ink is dark in BOTH renderings, so that is what to key on. Measured
    inside the discs on webgl: ink at p1=17 against a green disc body of 112,
    and p1=28 against a gold body of 198. Taking the darkest `INK_PCT` inside a
    disc-shaped window - which also excludes the parchment and the disc's own
    dark rim - yields a clean, legible glyph on either disc.

    IT NEEDS ITS OWN EXEMPLARS, and that was measured rather than assumed. The
    existing outline masks are NOT reusable: scored against ink masks of a
    known "0" and "1", every one of the sixteen sat at 0.33..0.63 distance and
    the ink "1" matched `2.png` best - a wrong answer. An outline and a
    silhouette of the same glyph are different shapes, which is also why this
    project's earlier attempts at normalising between the two measured worse.
    """
    x, y = int(xy[0]), int(xy[1])
    h0, w0 = frame.shape[:2]
    if not (r <= x < w0 - r and r <= y < h0 - r):
        return None
    g = cv2.cvtColor(frame[y - r:y + r, x - r:x + r], cv2.COLOR_BGR2GRAY)
    if g.size == 0:
        return None
    h, w = g.shape
    yy, xx = np.mgrid[0:h, 0:w]
    inside = ((xx - w / 2.0) ** 2 + (yy - h / 2.0) ** 2) <= \
        (min(h, w) * INK_DISC_FRAC) ** 2
    vals = g[inside]
    if vals.size < 50:
        return None
    cut = np.percentile(vals, INK_PCT)
    return ((g <= cut) & inside).astype(np.uint8) * 255


def tight_glyph(mask, pad=2):
    """Trim a binarised digit crop to the glyph itself.

    THIS IS WHAT MAKES THE MATCH WORK. An exemplar the same size as the patch
    gives `matchTemplate` exactly ONE position to score, so any misalignment is
    charged straight to the confidence - and the rows DO drift (CLAUDE.md: the
    history scroll must be located, not computed; measured pitch 88.53 against
    an assumed 88.0, ~25px over ten rows). Trimming the exemplar to its glyph and
    searching it inside the full patch gives the matcher room to find the offset.

    Measured on real crops of the SAME digit, before -> after:

        green "0" vs the 0 exemplar     0.311 -> 1.000
        gold  "1" vs the 1 exemplar     0.611 -> 1.000

    Both sat under the 0.80 gate before, which is exactly why a live run logged
    "could not read row 0 (green 0.524 / gold 0.611)" and abandoned a puzzle
    whose feedback was perfectly legible.

    Only the CENTRAL region is kept: the crop catches the edges of neighbouring
    discs, and those are as bright as the glyph outline.
    """
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return mask
    h, w = mask.shape
    cy, cx = h // 2, w // 2
    keep = (np.abs(ys - cy) < h * 0.42) & (np.abs(xs - cx) < w * 0.42)
    if keep.sum() < 10:
        return mask
    y0, y1 = ys[keep].min(), ys[keep].max()
    x0, x1 = xs[keep].min(), xs[keep].max()
    return mask[max(0, y0 - pad):y1 + pad + 1, max(0, x0 - pad):x1 + pad + 1]


def read_digit(frame, xy, exemplars, gate=0.80):
    """Classify a digit crop against saved exemplars. Returns (value, conf).

    Returns (None, best) when nothing clears `gate` — an unread counter must NOT
    be silently treated as a zero. A wrong 0 is indistinguishable from a real one
    and would corrupt the solver's model, which then converges on nothing.
    """
    if not exemplars:
        return None, 0.0
    patch = digit_mask(frame, xy)
    best, bestv = 0.0, None
    for val, imgs in exemplars.items():
        for img in (imgs if isinstance(imgs, list) else [imgs]):
            g = tight_glyph(img)
            if g.shape[0] > patch.shape[0] or g.shape[1] > patch.shape[1]:
                continue
            r = cv2.matchTemplate(patch, g, cv2.TM_CCOEFF_NORMED)
            m = float(cv2.minMaxLoc(r)[1])
            if m > best:
                best, bestv = m, val
    return (bestv, best) if best >= gate else (None, best)


def load_exemplars():
    """digit -> [exemplar, ...]. Filenames are "<digit>[_variant].png".

    Several exemplars per digit are needed because a row that has NOT been played
    renders its counters dimmer than a played row: the same "0" matched 1.000
    against a played-row exemplar and only 0.767 against an unplayed one, which is
    under the gate. Variants are cheaper and more honest than lowering the gate,
    which would start accepting cross-digit matches.
    """
    out = {}
    # THE INK DIRECTORY, not `digits/`. Those are outline masks for the old
    # bright-pixel mask and are measurably incompatible with the ink mask - see
    # `digit_mask`. They are left in place as the record of what wgpu draws,
    # and simply not loaded.
    for p in sorted(glob.glob(os.path.join(ROOT, INK_DIR, "*.png"))):
        n = os.path.splitext(os.path.basename(p))[0]
        head = n.split("_")[0]
        if not head.isdigit():
            continue                     # UNREAD_*: unclassified, never guess
        g = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if g is None:
            continue
        # Already a mask on disk - stored exactly as `digit_mask` produces it,
        # so no re-binarising. Re-thresholding a mask is how two
        # representations quietly stop being comparable.
        out.setdefault(int(head), []).append(g)
    return out


def enter_guess(actor, guess, settle=0.55, rune_xy=None):
    """Click the runes in order. Returns True if all were clicked.

    `rune_xy` should come from `locate_panel` - the panel MOVES, and the module
    constants are only a reference layout.
    """
    xy = rune_xy or RUNE_XY
    for rune in guess:
        if rune not in xy:
            return False
        actor.click_pixel(*xy[rune], why=f"rune {rune}")
        time.sleep(settle)
    return True


def solve_live(cap, actor, log, length=3, max_guesses=10, settle=2.2,
               history=None, on_history=None):
    """Solve the puzzle by playing it. Returns (secret, guesses) or (None, n).

    WHICH COUNTER IS WHICH IS NOT ASSUMED.
    The green disc is either "correct place" or "correct rune wrong place"; we do
    not know which, and getting it backwards makes the solver filter on inverted
    feedback and converge on nothing. So both mappings are carried as live
    hypotheses and consistency kills the wrong one: a hypothesis whose candidate
    pool goes empty is disproved. That costs no extra guesses.

    IT CAN NOW RESUME. `history` seeds the model with guesses already on the
    scroll as [(guess_tuple, (green, gold)), ...], and `on_history` is called
    with that list after every answer so a caller can keep it.

    **WHY THAT MATTERS: ROWS ARE THE SCARCE RESOURCE.** A stage allows ten
    guesses and failing to solve inside them fails the whole mission. Without
    resume, every restart replayed the same openers into fresh rows - and a
    restart is exactly what happens after an unreadable digit is harvested and
    labelled. Measured on an SS length-5 stage: two rows spent, then a stall;
    a naive re-run would have spent two more repeating itself, against an
    average requirement of about six.
    """
    ex = load_exemplars()
    if not ex:
        log.info("no digit exemplars in %s - cannot read feedback", INK_DIR)
        return None, 0
    pool_all = kekkai.candidates(length)
    hist_a, hist_b = [], []          # A: green=cp,gold=wp   B: green=wp,gold=cp
    alive_a = alive_b = True
    if history:
        # THE SAME SHAPE THIS FUNCTION ALREADY APPENDS - `(guess, green, gold)`,
        # a flat 3-tuple, which is what `kekkai.consistent` expects. The first
        # attempt seeded a nested `(guess, (green, gold))` and would have
        # filtered against a shape the solver never produces.
        for g, gv, ov in history:
            hist_a.append((g, gv, ov))
            hist_b.append((g, ov, gv))
        log.info("resuming with %d answer(s) already on the scroll", len(history))

    # The panel MUST already be open. Treating "no history discs" as success was
    # a bug that reported "solved after 0 guesses" when the puzzle had simply
    # never opened - a click had landed on the character's robe instead of a
    # kekkai. Absence of the panel before any guess means NOT OPEN, not solved.
    first = cap.frame(gray=False)
    if find_rows(first)[0] is None:
        log.info("kekkai puzzle is not open - nothing to solve. Open a kekkai "
                 "first; this is not a success.")
        return None, 0

    # Did this panel ALREADY have guesses in it when we arrived? It happens
    # whenever a previous attempt was abandoned mid-puzzle, and it changes how
    # much a first-guess panel closure is worth as evidence - see the guard
    # below.
    resumed = count_filled(first) > 0
    if resumed:
        log.info("resuming a puzzle that already has %d guess(es) of history",
                 count_filled(first))

    for n in range(max_guesses):
        frame = cap.frame(gray=False)
        gx, ys = find_rows(frame)
        if gx is None:
            # Disappeared AFTER at least one submitted guess -> genuinely solved.
            log.info("puzzle panel gone after %d guess(es) -> solved", n)
            return "solved", n

        pa = kekkai.consistent(pool_all, hist_a) if alive_a else []
        pb = kekkai.consistent(pool_all, hist_b) if alive_b else []
        if alive_a and not pa:
            alive_a = False
            log.info("hypothesis A (green=correct-place) disproved")
        if alive_b and not pb:
            alive_b = False
            log.info("hypothesis B (green=wrong-place) disproved")
        if not (alive_a or alive_b):
            log.info("both counter mappings contradicted - feedback misread")
            return None, n

        # Prefer a guess consistent with every surviving hypothesis.
        both = [c for c in pa if c in set(pb)] if (alive_a and alive_b) else []
        pool = both or (pa if alive_a else pb)
        guess = kekkai.next_guess(length, hist_a if alive_a else hist_b) \
            if len(pool) == len(pa or pb) else pool[0]
        if guess is None:
            guess = pool[0]
        log.info("guess %d: %s   (pool A=%d B=%d)", n + 1, ",".join(guess),
                 len(pa), len(pb))

        # LOCATE THE PANEL EVERY GUESS. It is not where the constants say - it
        # was measured 116 px higher - and the discs are only r~55, so stale
        # coordinates miss the buttons entirely, fill no slots, and submit
        # nothing. That failure is SILENT: the history simply stays empty.
        rune_xy, confirm_xy = locate_panel(cap.frame(gray=False), log, cap)
        enter_guess(actor, guess, rune_xy=rune_xy)
        time.sleep(0.5)
        actor.click_pixel(*confirm_xy, why="confirm guess")
        time.sleep(settle)

        frame = cap.frame(gray=False)
        gx2, ys2 = find_rows(frame)
        if gx2 is None:
            # The panel closes the instant a guess is right, so this is success -
            # and it must be checked BEFORE reading digits, or we read a closed
            # panel, score 0.000 and report failure on a solved puzzle.
            #
            # BUT closure is only EVIDENCE of success, not proof, and it is at
            # its weakest on the very first guess of a RESUMED panel. Observed
            # live: a run reconnected to a puzzle that already had two guesses in
            # its history, misread the node count, submitted five identical
            # runes, the panel closed, and it reported "SOLVED in 1 guess" for a
            # 1-in-7776 code. Corroborate with the seal-broken dialog, which a
            # real solve always raises.
            if n == 0 and resumed and not seal_broken(cap):
                log.info("panel closed on the FIRST guess of a resumed puzzle "
                         "and no seal-broken dialog appeared - treating that as "
                         "the old panel being dismissed, NOT a solve")
                return None
            log.info("panel closed after guess %d -> SOLVED: %s", n + 1,
                     ",".join(guess))
            return guess, n + 1
        # DID THE GUESS ACTUALLY REGISTER? Only ask this with the panel STILL
        # OPEN. A correct guess closes the panel instantly, and a closed panel
        # has no filled rows - so asking first reported "did not register" for a
        # puzzle that had just been SOLVED, and abandoned it. That is precisely
        # the trap this file already documents for digit reading, walked into
        # again one branch higher up.
        #
        # With the panel open, no new row means the rune clicks did not land.
        # Reading "the last filled row" would then read row 0, an UNPLAYED row,
        # whose dim 0/0 is unclassifiable - which is how a mission was burned
        # with ten identical unplayed rows while the solver blamed its digit
        # exemplars.
        if count_filled(frame) <= len(hist_a):
            log.info("guess %d did not register - no new row appeared. The rune "
                     "clicks did not land (panel moved?); abandoning rather "
                     "than filling the history with phantom rows", n + 1)
            return None, n + 1

        # Read the row that was just filled, located by counting filled rows -
        # NOT by assuming it is row len(history).
        g_xy, o_xy = row_center(max(0, count_filled(frame) - 1), frame)
        gv, gc = read_digit(frame, g_xy, ex)
        ov, oc = read_digit(frame, o_xy, ex)
        if gv is None or ov is None:
            d = os.path.join(ROOT, INK_DIR)
            os.makedirs(d, exist_ok=True)
            cv2.imwrite(os.path.join(d, f"UNREAD_green_{n}.png"),
                        digit_mask(frame, g_xy))
            cv2.imwrite(os.path.join(d, f"UNREAD_gold_{n}.png"),
                        digit_mask(frame, o_xy))
            # SAVE THE WHOLE FRAME, not only the two crops.
            #
            # The crops alone cannot tell you WHY the read failed, and that
            # cost a diagnosis: both came back as neither digit nor noise - one
            # was almost solid black, the other a white SPIRAL, which is a RUNE
            # GLYPH. So the reader was not misreading a digit at all, it was
            # looking in the wrong place, and nothing in a 68x68 binarised crop
            # says that. `find_rows` segments a fixed absolute column
            # (x 1950..2030) by GREEN HUE, and both of those are fragile here:
            # the panel moves (measured 116 px on one occasion) and colour is
            # exactly what a different Ruffle backend renders differently.
            #
            # With the full frame, where the discs really are is measurable
            # afterwards. Bounded, because the point is one good frame.
            try:
                nsaved = len([f for f in os.listdir(d)
                              if f.startswith("UNREAD_frame")])
                if nsaved < 3:
                    fp = os.path.join(d, f"UNREAD_frame_{int(time.time())}.png")
                    cv2.imwrite(fp, frame)
                    log.info("saved the whole panel to %s - measure where the "
                             "feedback discs actually are before touching the "
                             "digit exemplars", os.path.relpath(fp, ROOT))
            except Exception as e:
                log.warning("could not save the panel frame: %s", e)
            log.info("could not read row %d (green %.3f / gold %.3f) at "
                     "green=%s gold=%s; crops saved as UNREAD_*. Classify them "
                     "and rerun rather than guessing.",
                     len(hist_a), gc, oc, g_xy, o_xy)
            if on_history is not None:
                on_history(list(hist_a))
            return None, n + 1
        log.info("   feedback: green=%d gold=%d", gv, ov)
        if gv == length or ov == length:
            log.info("   a counter reached %d -> SOLVED: %s", length,
                     ",".join(guess))
            return guess, n + 1
        hist_a.append((guess, gv, ov))
        hist_b.append((guess, ov, gv))
        if on_history is not None:
            on_history(list(hist_a))
    return None, max_guesses


# Walking targets, captured px. THESE ARE THE MAP EDGES, deliberately.
#
# The traversal is not a scroll within one scene: if no kekkai is on the current
# map you have to RUN TO AN EDGE, and the location changes during the running
# sequence. So sweeping mid-ground points (which is what I tried first) just
# shuffles the character around one map forever and finds nothing.
#
# The game canvas occupies captured x 760..2680 at the standard viewport, so the
# edges are just inside those bounds. Alternate right/left so a dead end at one
# edge is followed by the other.
CANVAS_X0, CANVAS_X1 = 760, 2680
GROUND_Y = 880
EDGE_RIGHT = (CANVAS_X1 - 40, GROUND_Y)
EDGE_LEFT = (CANVAS_X0 + 40, GROUND_Y)

# A run to the edge plus the location change takes noticeably longer than a
# short walk, and scanning mid-transition gives a false "nothing here".
EDGE_SETTLE = 4.5

# How different two frames must be to count as a MAP CHANGE rather than the
# character having merely moved within the same map. The scene art is almost
# entirely replaced on a transition, so this is a loose threshold.
MAP_CHANGE_DIFF = 0.18


def seal_broken(cap, settle=1.2):
    """Is the "You break the seal!" dialog on screen?

    Corroboration for a solve. The dialog's acknowledge control is the same green
    check the rest of the game uses, drawn at yet another size, so the search
    sweeps scale like everywhere else.
    """
    from perceive import Template, find
    time.sleep(settle)
    g = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
    # Named by its FILE (`mission_start`), because the renderer-variant
    # lookup is by template name - "gc" would find no variant.
    t = perceive.template("mission_start", threshold=0.80)
    t.scales = [round(0.95 + i * 0.05, 2) for i in range(21)]
    return find(g, t)[0].found


def mission_over(cap, log=None):
    """Is the mission finished — i.e. is there nothing left to hunt?

    Checked between rounds, never mid-puzzle. Either anchor means the run is
    done: the Success panel is the end of the mission, and the epilogue cutscene
    only appears once the last seal is broken.
    """
    from perceive import Template, find
    g = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
    for name, thr in (("mission_success", 0.88), ("cutscene_continue", 0.80)):
        t = perceive.template(name, threshold=thr)
        if t is None:
            continue
        if name == "cutscene_continue":
            t.scales = [round(0.9 + i * 0.05, 2) for i in range(9)]
        m, c = find(g, t)
        if m.found:
            if log:
                log.info("mission-over anchor %s (%.3f)", name, c)
            return True
    return False


def hunt_and_solve(cap, actor, log, length=3, max_rounds=6, max_walks=10):
    """Find each kekkai in the scene, solve it, repeat.

    The mission is not over when one seal breaks - the HUD reads `Seals: 1 / 2`.
    So this alternates LOCATE -> OPEN -> SOLVE -> ACKNOWLEDGE until no kekkai can
    be found any more.
    """
    solved = 0
    heading = heading_from_spawn(cap.frame(gray=False), log, cap)
    for rnd in range(max_rounds):
        # STOP AS SOON AS THE MISSION IS OVER. Breaking the last seal ends the
        # mission, and what follows is an epilogue cutscene and the Success
        # panel - not another map to search. Without this check the hunt kept
        # going after "Seals: 2 / 2", ran to a map edge that no longer existed,
        # found a "seal" that was panel artwork, and logged "could not open the
        # seal after 3 attempts" on a mission it had already won. Harmless here
        # only because the clicks landed on a panel; the class of bug is
        # clicking blindly on an unrecognised screen.
        if mission_over(cap, log):
            log.info("the mission has ended; %d seal(s) solved this run", solved)
            return solved
        target = None
        for w in range(max_walks):
            frame = cap.frame(gray=False)
            target = find_kekkai(frame)
            if target:
                log.info("round %d: seal on this map at %s (after %d run(s))",
                         rnd + 1, target, w)
                break

            # Heading comes from where the character is STANDING, not from a
            # fixed default and not merely from persistence. See
            # heading_from_spawn: you spawn near the edge you entered through, so
            # you must head away from it, and that has to be re-derived on every
            # new map.
            # The edge is computed from the LIVE canvas, not the reference
            # constants: focus mode flush-lefts the game, so a target of
            # x=2640 would land outside a canvas that now ends at 1920.
            _cx0, _cx1 = canvas_x(cap)
            edge = ((_cx1 - 40, GROUND_Y) if heading == "right"
                    else (_cx0 + 40, GROUND_Y))
            log.info("round %d: no seal here; running %s to the edge %s",
                     rnd + 1, heading, edge)
            # Movement is judged by WHERE THE CHARACTER IS, not by a whole-frame
            # diff. A real map change measured only 0.053 mean-abs-diff because
            # the scenes are similarly lit, so a 0.18 threshold called every
            # successful transition a dead end and the hunt oscillated forever.
            # The character's x jumps a long way on a transition, which is a far
            # cleaner signal.
            x_before = (find_character(frame) or (None, None))[0]
            actor.click_pixel(*edge, why=f"run {heading} to map edge")
            time.sleep(EDGE_SETTLE)
            after_f = cap.frame(gray=False)
            x_after = (find_character(after_f) or (None, None))[0]
            moved = (x_before is not None and x_after is not None
                     and abs(x_after - x_before) >= 150)
            if moved or find_kekkai(after_f):
                heading = heading_from_spawn(after_f, log, cap)
                log.info("   moved (x %s -> %s)", x_before, x_after)
            else:
                heading = "left" if heading == "right" else "right"
                log.info("   did not move (x %s -> %s) - dead end, turning %s",
                         x_before, x_after, heading)
        if not target:
            log.info("no further seal found; %d solved this run", solved)
            return solved

        # Node count must be taken NOW, while the seal is still drawn in the
        # scene. Reading it after opening the puzzle returns nothing, because the
        # seal is no longer on screen - which silently fell back to the default
        # length and made a 5-node seal be solved as a 3-rune code.
        nodes = count_nodes(cap.frame(gray=False), target)
        use_len = nodes if nodes and 2 <= nodes <= 6 else length
        log.info("seal node count = %s -> code length %d", nodes, use_len)

        # RUN TOWARDS THE SEAL FIRST, then open it. A seal across the map is not
        # directly clickable - the first click walks us there, and only once we
        # are beside it does a click open the puzzle.
        opened = False
        for attempt in range(3):
            actor.click_pixel(*target, why=f"run to / open seal (try {attempt+1})")
            time.sleep(2.6)
            if find_rows(cap.frame(gray=False))[0] is not None:
                opened = True
                break
            again = find_kekkai(cap.frame(gray=False))
            if again:
                target = again          # re-aim; we may have moved closer
        if not opened:
            log.info("could not open the seal after 3 attempts; stopping")
            return solved

        secret, used = solve_live(cap, actor, log, length=use_len)
        if not secret:
            log.info("solver did not finish this kekkai; stopping")
            return solved
        solved += 1
        log.info("kekkai %d solved in %d guess(es)", solved, used)

        # acknowledge "You break the seal!"
        time.sleep(1.5)
        f = cap.frame(gray=False)
        from perceive import find as _find
        gc = perceive.template("mission_start", threshold=0.80)
        gc.scales = [round(0.95 + i * 0.05, 2) for i in range(21)]
        m, conf = _find(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), gc)
        if m.found:
            actor.click_pixel(*m.center, why="acknowledge broken seal")
            time.sleep(2.5)
    return solved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--length", type=int, default=3)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--guess", help="comma-separated runes to enter, then stop")
    ap.add_argument("--dump-row", type=int,
                    help="save the two digit crops for this row (0-based) and exit")
    ap.add_argument("--shot", action="store_true", help="just save a frame")
    ap.add_argument("--solve", action="store_true", help="play the puzzle to a solution")
    ap.add_argument("--hunt", action="store_true",
                    help="locate every kekkai in the scene and solve each")
    ap.add_argument("--seed", help="feedback already on screen: 'Green,Red,Blue:0,1'")
    a = ap.parse_args()
    log = _Log()

    t = find_page_target(port=a.port, url_contains="ninjasaga", timeout=20)
    c = CDP(t["webSocketDebuggerUrl"])
    c.call("Page.enable")
    cap = Capture(c)
    actor = Actor(c, cap, log, dry_run=False,
                  click_delay=(0.08, 0.16), post_click=(0.12, 0.25))

    if a.shot:
        f = cap.frame(gray=False)
        cv2.imwrite(os.path.join(ROOT, "ref/auto/tp/kekkai_now.png"), f)
        print("saved ref/auto/tp/kekkai_now.png")
        c.close()
        return 0

    if a.dump_row is not None:
        f = cap.frame(gray=False)
        d = os.path.join(ROOT, "ref/auto/tp/digits")
        os.makedirs(d, exist_ok=True)
        g_xy, o_xy = row_center(a.dump_row)
        for tag, xy in (("green", g_xy), ("gold", o_xy)):
            crop = crop_digit(f, xy)
            p = os.path.join(d, f"row{a.dump_row}_{tag}.png")
            cv2.imwrite(p, crop)
            big = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
            cv2.imwrite(os.path.join(d, f"row{a.dump_row}_{tag}_big.png"), big)
            print(f"  {tag} @ {xy} -> {p}")
        c.close()
        return 0

    if a.hunt:
        n = hunt_and_solve(cap, actor, log, length=a.length)
        print(f"\nsolved {n} kekkai")
        c.close()
        return 0 if n else 1

    if a.solve:
        secret, used = solve_live(cap, actor, log, length=a.length)
        print(f"\nresult: {secret}  after {used} guess(es)")
        c.close()
        return 0 if secret else 1

    if a.guess:
        g = [x.strip() for x in a.guess.split(",") if x.strip()]
        print("entering:", g)
        enter_guess(actor, g)
        time.sleep(1.2)
        f = cap.frame(gray=False)
        cv2.imwrite(os.path.join(ROOT, "ref/auto/tp/kekkai_after_guess.png"), f)
        print("saved ref/auto/tp/kekkai_after_guess.png")
        c.close()
        return 0

    print("nothing to do; pass --guess / --dump-row / --shot")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
