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


def find_character(frame, x0=800, x1=2650, y0=200, y1=1150,
                   min_area=1500, max_area=9000, max_aspect=0.80, min_fill=0.22):
    """Locate our own character by its red robe. Returns (x, y) or None.

    This is the exact blob that fooled the kekkai detector, so its signature is
    already measured: area 4040, bbox 65x170, fill 0.366, aspect 0.38. It is the
    inverse of a kekkai - small, TALL and fairly solid, where a kekkai is large,
    wide and sparse. Reusing the same segmentation for both means one colour pass
    tells us where we are AND where the seal is.
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
        if not (min_area <= a <= max_area):
            continue
        w, h = st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
        if w * h == 0 or w / max(1, h) > max_aspect or a / (w * h) < min_fill:
            continue
        if best is None or a > best[0]:
            best = (a, (x0 + int(ce[i][0]), y0 + int(ce[i][1])))
    return best[1] if best else None


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


def heading_from_spawn(frame, log=None):
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
    centre = (CANVAS_X0 + CANVAS_X1) // 2
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


def find_rows(frame, x0=1950, x1=2030, y0=240, y1=1200, min_area=800,
              min_rows=5):
    """Locate the feedback rows by segmenting the GREEN disc column.

    Returns (green_x, [y, ...]) top to bottom, or (None, []) if the panel is not
    open. Measured on our client: 10 rows, y 290..1087, pitch 88.53, green column
    x 1987.

    `min_rows` exists because ONE stray green blob is not a history scroll. After
    a correct guess the panel closes instantly, and a single unrelated green
    element elsewhere on the scene made this report "panel open, 1 row" - so the
    solver went on to read digits out of a closed panel, got 0.000, and bailed
    with "could not read" on a puzzle it had in fact just solved.
    """
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


def count_filled(frame, x0=2120, x1=2230, box=30):
    """How many history rows already hold a guess.

    Needed because reading "the row for my Nth guess" as row N-1 is wrong the
    moment the scroll already has entries — e.g. a guess entered by hand before
    the solver started. That off-by-one made the solver read guess 2's feedback
    off guess 1's row and corrupted its whole model.

    A filled row shows coloured RUNE ICONS where an empty one shows only a dash.
    MEAN saturation does not separate them - parchment is itself fairly saturated,
    so filled rows read 87..93 and empty ones 47..53, and any single mean cutoff
    is fragile. The FRACTION of strongly-saturated pixels does separate cleanly:
    measured 0.243..0.303 for filled rows against 0.000..0.028 for empty.
    """
    gx, ys = find_rows(frame)
    if gx is None:
        return 0
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    n = 0
    for y in ys:
        cell = hsv[y - box:y + box, x0:x1]
        if cell.size and float((cell[:, :, 1] > 90).mean()) > 0.12:
            n += 1
    return n


def row_center(i, frame=None):
    """(green_xy, gold_xy) for history row i, 0-based. Located when given a frame."""
    if frame is not None:
        gx, ys = find_rows(frame)
        if gx is not None and i < len(ys):
            return (gx, ys[i]), (gx + HIST_GOLD_DX, ys[i])
    # fallback to the measured grid if segmentation failed
    y = int(round(290 + i * 88.53))
    return (1987, y), (1987 + HIST_GOLD_DX, y)


def crop_digit(frame, xy):
    x, y = xy
    return frame[y - DIGIT_BOX:y + DIGIT_BOX, x - DIGIT_BOX:x + DIGIT_BOX]


def digit_mask(frame, xy):
    """Binarised digit crop.

    The glyph is a DARK digit with a WHITE OUTLINE on a coloured disc (green for
    one counter, gold for the other). The white outline is the only
    colour-independent feature, so thresholding bright pixels lets ONE exemplar
    set serve both discs. Measured: self-match 1.000, cross-digit 0.161.
    """
    g = cv2.cvtColor(crop_digit(frame, xy), cv2.COLOR_BGR2GRAY)
    return cv2.threshold(g, 200, 255, cv2.THRESH_BINARY)[1]


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
            if img.shape[0] > patch.shape[0] or img.shape[1] > patch.shape[1]:
                continue
            r = cv2.matchTemplate(patch, img, cv2.TM_CCOEFF_NORMED)
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
    for p in sorted(glob.glob(os.path.join(ROOT, "ref/auto/tp/digits/*.png"))):
        n = os.path.splitext(os.path.basename(p))[0]
        head = n.split("_")[0]
        if head.isdigit():
            out.setdefault(int(head), []).append(
                cv2.cvtColor(cv2.imread(p), cv2.COLOR_BGR2GRAY))
    return out


def enter_guess(actor, guess, settle=0.55):
    """Click the runes in order. Returns True if all were clicked."""
    for rune in guess:
        if rune not in RUNE_XY:
            return False
        actor.click_pixel(*RUNE_XY[rune], why=f"rune {rune}")
        time.sleep(settle)
    return True


def solve_live(cap, actor, log, length=3, max_guesses=10, settle=2.2):
    """Solve the puzzle by playing it. Returns (secret, guesses) or (None, n).

    WHICH COUNTER IS WHICH IS NOT ASSUMED.
    The green disc is either "correct place" or "correct rune wrong place"; we do
    not know which, and getting it backwards makes the solver filter on inverted
    feedback and converge on nothing. So both mappings are carried as live
    hypotheses and consistency kills the wrong one: a hypothesis whose candidate
    pool goes empty is disproved. That costs no extra guesses.
    """
    ex = load_exemplars()
    if not ex:
        log.info("no digit exemplars in ref/auto/tp/digits/ - cannot read feedback")
        return None, 0
    pool_all = kekkai.candidates(length)
    hist_a, hist_b = [], []          # A: green=cp,gold=wp   B: green=wp,gold=cp
    alive_a = alive_b = True

    # The panel MUST already be open. Treating "no history discs" as success was
    # a bug that reported "solved after 0 guesses" when the puzzle had simply
    # never opened - a click had landed on the character's robe instead of a
    # kekkai. Absence of the panel before any guess means NOT OPEN, not solved.
    if find_rows(cap.frame(gray=False))[0] is None:
        log.info("kekkai puzzle is not open - nothing to solve. Open a kekkai "
                 "first; this is not a success.")
        return None, 0

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

        enter_guess(actor, guess)
        time.sleep(0.5)
        actor.click_pixel(*CONFIRM_XY, why="confirm guess")
        time.sleep(settle)

        frame = cap.frame(gray=False)
        gx2, ys2 = find_rows(frame)
        if gx2 is None:
            # The panel closes the instant a guess is right, so this is success -
            # and it must be checked BEFORE reading digits, or we read a closed
            # panel, score 0.000 and report failure on a solved puzzle.
            log.info("panel closed after guess %d -> SOLVED: %s", n + 1,
                     ",".join(guess))
            return guess, n + 1
        # Read the row that was just filled, located by counting filled rows -
        # NOT by assuming it is row len(history).
        g_xy, o_xy = row_center(max(0, count_filled(frame) - 1), frame)
        gv, gc = read_digit(frame, g_xy, ex)
        ov, oc = read_digit(frame, o_xy, ex)
        if gv is None or ov is None:
            d = os.path.join(ROOT, "ref/auto/tp/digits")
            cv2.imwrite(os.path.join(d, f"UNREAD_green_{n}.png"),
                        digit_mask(frame, g_xy))
            cv2.imwrite(os.path.join(d, f"UNREAD_gold_{n}.png"),
                        digit_mask(frame, o_xy))
            log.info("could not read row %d (green %.3f / gold %.3f); crops saved "
                     "as UNREAD_*. Classify them and rerun rather than guessing.",
                     len(hist_a), gc, oc)
            return None, n + 1
        log.info("   feedback: green=%d gold=%d", gv, ov)
        if gv == length or ov == length:
            log.info("   a counter reached %d -> SOLVED: %s", length,
                     ",".join(guess))
            return guess, n + 1
        hist_a.append((guess, gv, ov))
        hist_b.append((guess, ov, gv))
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


def hunt_and_solve(cap, actor, log, length=3, max_rounds=6, max_walks=10):
    """Find each kekkai in the scene, solve it, repeat.

    The mission is not over when one seal breaks - the HUD reads `Seals: 1 / 2`.
    So this alternates LOCATE -> OPEN -> SOLVE -> ACKNOWLEDGE until no kekkai can
    be found any more.
    """
    solved = 0
    heading = heading_from_spawn(cap.frame(gray=False), log)
    for rnd in range(max_rounds):
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
            edge = EDGE_RIGHT if heading == "right" else EDGE_LEFT
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
                heading = heading_from_spawn(after_f, log)
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
        from perceive import Template, find as _find
        gc = Template("gc", os.path.join(ROOT, "tpl/mission_start.png"),
                      threshold=0.80)
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
