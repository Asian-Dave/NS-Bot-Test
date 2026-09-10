#!/usr/bin/env python3
"""Balance Control — SS Training's subset-swap puzzle, and it is ON A CLOCK.

WHAT THE BOARD IS
-----------------
Two columns of numbers with a circle button on every row between them, a red
SUM box under each column, and `Target: N` at the top. Measured on stage 1 of a
live mission:

    Stage 1   169s      Target: 50
    left      17 18  7 12   ->  54
    right      5 24 15  2   ->  46

Clicking a row's circle SWAPS that row's two numbers, so the grand total is
invariant and the target is forced:

    target == (left_sum + right_sum) // 2

which is why nothing here reads the white `Target:` text. Deriving it costs no
perception at all and cannot disagree with the board; reading it would add a
third rendering of the digits (white, on the background rather than in a box)
for no information. The parity check `(l + r) % 2 == 0` is kept as the honest
statement of "this is the puzzle I think it is".

So the puzzle is a SUBSET SUM. With `d_i = right_i - left_i`, choosing the set
S of rows to swap moves the left sum by `sum(d_i for i in S)`, and the goal is

    sum(d_i for i in S) == target - left_sum

Stage 1's board has two solutions - rows {0,2} (-12+8) and rows {1,3}
(+6-10) - so `solve` returns the SMALLEST set and there is no need to be
clever. Four rows is sixteen subsets; brute force is the whole algorithm.

THE CLOCK IS THE REASON THIS MODULE EXISTS AT ALL
-------------------------------------------------
The first live look at this board cost the mission: reading it, measuring the
geometry offline and coming back to click took longer than the 169 s timer, and
the stage ended `Mission Fail` at `0s` with nothing clicked. Every step here is
therefore in one pass - locate, read, solve, click - and the module never waits
on anything it could compute.

READING THE DIGITS
------------------
The row numbers are pale yellow and the sums are RED, but they are the same
glyphs at the same size, so both are binarised to a mask and ONE exemplar set
serves both - the same trick the kekkai counters needed for their outlined and
solid renderings. Measured across the ten boxes of stage 1, every digit was
read with a worst margin of 5.8x and a best-match distance of 0.000..0.037.

`ref/auto/ss/balance_digits/` starts with 1 2 4 5 6 7 8, harvested from that
board. 0, 3 and 9 have not been seen yet, and an unread digit is reported
rather than guessed - **the arithmetic is the verdict**: the four row values
must add up to the sum box, and a board that does not add up is not acted on.
That check is free, it is exactly the invariant a misread breaks, and this
project has already paid for a misread digit poisoning a solver's model.
"""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGIT_DIR = os.path.join(ROOT, "ref/auto/ss/balance_digits")

# --- colour ---------------------------------------------------------------
# Pale yellow row numbers measured at HSV (30, 90, 255) - a bright, only
# moderately saturated cream. The sums are pure red, and the mask has to
# straddle the hue wrap.
PALE = ((18, 40, 190), (42, 190, 255))
RED_LO = ((0, 110, 110), (9, 255, 255))
RED_HI = ((172, 110, 110), (180, 255, 255))

# --- geometry -------------------------------------------------------------
# Measured on the live board: number boxes 359x157 at row pitch 203, columns
# centred x=1236 and x=2188, the sum row's digits centred y=1343. Only the
# PITCH is carried as a constant; the columns and the sum row are LOCATED from
# the red digits every time, because every absolute geometry in this project
# has eventually been moved by a reflow.
PITCH = 203
BOX_HALF_W = 175
BOX_HALF_H = 70
MAX_ROWS = 8

# A sum digit: 30..80 wide, 75..105 tall, 1000..4000 px of ink. The bounds
# exist to reject the two other red things on screen - the Admin Message
# banner (490x80, 38,773 px) and a `Mission Fail` title.
SUM_DIGIT_W = (30, 82)
SUM_DIGIT_H = (72, 108)
SUM_DIGIT_A = (900, 4200)
COL_GAP = 300              # least distance between the two columns' digits
SUM_ROW_TOL = 20           # the two columns' bottom rows share a baseline
MIN_ROWS = 2               # paired pale rows needed above the sums
BOX_DARK = 40              # box interior measured grey 31, border 77
BOX_W = (300, 430)         # measured 359
BOX_H = (120, 200)         # measured 157
PITCH_TOL = 12

DIGIT_SZ = (32, 48)        # exemplars are compared at this size
DIGIT_MAX_D = 0.10         # worst accepted distance; measured worst read 0.037
DIGIT_MIN_MARGIN = 2.0     # measured worst margin 5.8x

HIDDEN = "?"            # what a hidden sum box reads as

_EX = None


def _masks(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    pale = cv2.inRange(hsv, np.array(PALE[0], np.uint8),
                       np.array(PALE[1], np.uint8))
    red = cv2.inRange(hsv, np.array(RED_LO[0], np.uint8),
                      np.array(RED_LO[1], np.uint8)) | \
        cv2.inRange(hsv, np.array(RED_HI[0], np.uint8),
                    np.array(RED_HI[1], np.uint8))
    return pale, red


def _norm(m):
    return cv2.resize(m, DIGIT_SZ,
                      interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0


def exemplars():
    """{label: [mask, ...]}, loaded once. Empty if none have been harvested.

    One label is not a digit: `q_*.png` is the `?` the game draws in a HIDDEN
    sum box. It is an exemplar rather than a fallback, so "the sum is
    deliberately hidden" is read POSITIVELY and never confused with "a digit I
    could not recognise" - the two want opposite responses.
    """
    global _EX
    if _EX is None:
        _EX = {}
        for p in sorted(glob.glob(os.path.join(DIGIT_DIR, "*.png"))):
            d = os.path.basename(p)[0]
            if d == "q":
                d = HIDDEN
            elif not d.isdigit():
                continue
            im = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
            if im is not None:
                _EX.setdefault(d, []).append(_norm(im))
    return _EX


def _digits(mask, cx, cy):
    """The digit masks inside one box, left to right."""
    h, w = mask.shape[:2]
    x0, x1 = max(0, cx - BOX_HALF_W), min(w, cx + BOX_HALF_W)
    y0, y1 = max(0, cy - BOX_HALF_H), min(h, cy + BOX_HALF_H)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    sub = mask[y0:y1, x0:x1]
    n, lab, st, _ = cv2.connectedComponentsWithStats(sub)
    out = []
    for i in range(1, n):
        x, y, bw, bh, a = st[i]
        if a < 250 or bh < 40:
            continue
        out.append((int(x), (lab[y:y + bh, x:x + bw] == i).astype(np.uint8) * 255))
    out.sort(key=lambda t: t[0])
    return [c for _, c in out]


def read_number(mask, cx, cy, keep=None):
    """The integer in one box, `HIDDEN` for `??`, or None if it did not read.

    `keep` collects (label_or_None, crop) so an unseen digit can be harvested
    from the frame that defeated us - the trick that eventually solved every
    other unreadable screen in this project.
    """
    crops = _digits(mask, cx, cy)
    if not crops:
        return None
    ex = exemplars()
    s = ""
    for c in crops:
        q = _norm(c)
        scored = sorted((float(np.abs(q - e).mean()), d)
                        for d, es in ex.items() for e in es)
        if not scored:
            if keep is not None:
                keep.append((None, c))
            return None
        best_d, best = scored[0]
        second = next((d for d, lb in scored if lb != best), None)
        margin = (second / best_d) if best_d > 1e-6 else 1e9
        if best_d > DIGIT_MAX_D or margin < DIGIT_MIN_MARGIN:
            if keep is not None:
                keep.append((None, c))
            return None
        if keep is not None:
            keep.append((best, c))
        s += best
    if HIDDEN in s:
        return HIDDEN
    return int(s)


def locate(frame):
    """(left_x, right_x, sum_row_y) from the number BOXES, or None.

    ANCHORED ON THE BOXES, NOT ON THE SUM DIGITS, and that is not a style
    choice. The first version keyed on the red sum text - which works right up
    until **stage 2, where the sums read `??`** and must be computed from the
    rows instead. The boxes are the one thing both variants draw.

    They are near-black rectangles: measured interior grey 31 against a border
    of 77, 359x157 at a 203 px row pitch, columns centred x=1236 and x=2188.
    A `grey < 40` pass finds the bottom THREE rows on every frame held and
    never the top two - the background glow behind those is brighter - which
    costs nothing, because the bottom row plus the pitch locates the rest.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    m = (gray < BOX_DARK).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, _lab, st, ce = cv2.connectedComponentsWithStats(m)
    found = []
    for i in range(1, n):
        bw, bh = int(st[i, cv2.CC_STAT_WIDTH]), int(st[i, cv2.CC_STAT_HEIGHT])
        if not (BOX_W[0] <= bw <= BOX_W[1] and BOX_H[0] <= bh <= BOX_H[1]):
            continue
        found.append((int(ce[i][0]), int(ce[i][1])))
    if len(found) < 4:
        return None
    found.sort()
    # Two columns, split at the widest x gap.
    gaps = [(found[i + 1][0] - found[i][0], i) for i in range(len(found) - 1)]
    g, i = max(gaps)
    if g < COL_GAP:
        return None
    left, right = found[:i + 1], found[i + 1:]
    if len(left) < 2 or len(right) < 2:
        return None
    lx = int(round(sum(p[0] for p in left) / len(left)))
    rx = int(round(sum(p[0] for p in right) / len(right)))
    # THE ROWS MUST BE EVENLY PITCHED. Two columns of dark rectangles is not
    # rare; two columns of rectangles on a shared 203 px ladder is.
    ys = sorted({p[1] for p in left})
    if len(ys) < 2:
        return None
    for a, b in zip(ys, ys[1:]):
        if abs((b - a) - PITCH) > PITCH_TOL:
            return None
    ry = sorted({p[1] for p in right})
    if abs(max(ry) - max(ys)) > SUM_ROW_TOL:
        return None
    return lx, rx, max(ys)


def board_present(frame):
    """`locate`'s geometry, but only for a frame that really holds the board.

    `locate` answers "where are the two sum boxes", and that question alone is
    not enough to DISPATCH on - `identify` uses this instead, because calling a
    battle a balance puzzle would hand a fight to the wrong player entirely.

    The structural test is the puzzle's own shape: at least two ROWS of paired
    pale numbers above the sums, at the measured 203 px pitch. Nothing else in
    this game draws that.
    """
    geom = locate(frame)
    if geom is None:
        return None
    lx, rx, sy = geom
    pale, _red = _masks(frame)
    rows = 0
    for i in range(1, MAX_ROWS + 1):
        y = sy - i * PITCH
        if y - BOX_HALF_H < 0:
            break
        if _digits(pale, lx, y) and _digits(pale, rx, y):
            rows += 1
        else:
            break
    return geom if rows >= MIN_ROWS else None


def read_board(frame, log=None, keep=None):
    """(rows, left_sum, right_sum, geom) or None.

    `rows` is a list of (left, right) top-down; `geom` is `locate`'s tuple.
    The row COUNT is measured by walking up from the sum row until a box has
    no digits in it - stage 1 had four, and a stage that has five must not be
    read as four.

    **THE SUMS ARE COMPUTED FROM THE ROWS, NOT READ.** Stage 2 draws them as
    `??` - the arithmetic is the puzzle there - so a reader that depends on
    them stops dead on the second stage of every mission. Adding them up is
    also the only definition that works for both variants.

    Where the sums ARE printed they become a free CHECK ON THE READING: a
    column must equal its own sum box, and this project has already paid for a
    misread digit poisoning a solver's model. When they are hidden that check
    is simply unavailable, and the round's own verdict (a board only re-rolls
    on success) carries the risk instead.
    """
    geom = locate(frame)
    if geom is None:
        if log:
            log.info("balance: no board - the number boxes are not on screen")
        return None
    lx, rx, sy = geom
    pale, red = _masks(frame)
    rows = []
    for i in range(1, MAX_ROWS + 1):
        y = sy - i * PITCH
        if y - BOX_HALF_H < 0:
            break
        lv = read_number(pale, lx, y, keep)
        rv = read_number(pale, rx, y, keep)
        if lv is None and rv is None:
            break
        if lv is None or rv is None or HIDDEN in (lv, rv):
            if log:
                log.info("balance: the row at y=%d read (%s, %s) - refusing "
                         "the board rather than guessing", y, lv, rv)
            return None
        rows.append((lv, rv))
    rows.reverse()
    if not rows:
        return None
    gl, gr = sum(r[0] for r in rows), sum(r[1] for r in rows)

    ls = read_number(red, lx, sy)
    rs = read_number(red, rx, sy)
    if ls == HIDDEN or rs == HIDDEN:
        if log:
            log.info("balance: the sums are hidden (??) - computing them from "
                     "the rows: %d/%d", gl, gr)
    elif ls is None or rs is None:
        if log:
            log.info("balance: the sum boxes hold something that is neither a "
                     "number nor ?? - refusing the board")
        return None
    elif (gl, gr) != (ls, rs):
        if log:
            log.info("balance: the board does not add up - the rows give %d/%d "
                     "but the sums read %d/%d, so a digit is wrong",
                     gl, gr, ls, rs)
        return None
    return rows, gl, gr, geom


def solve(rows, left_sum, right_sum):
    """The smallest set of row indices to swap, or None.

    Brute force over every subset. `rows` is at most a handful, and the target
    is forced by the total because a swap preserves it.
    """
    total = left_sum + right_sum
    if total % 2:
        return None                      # not balanceable; not this puzzle
    want = total // 2 - left_sum
    n = len(rows)
    best = None
    for bits in range(1 << n):
        s = 0
        for i in range(n):
            if bits >> i & 1:
                s += rows[i][1] - rows[i][0]
        if s != want:
            continue
        idx = [i for i in range(n) if bits >> i & 1]
        if best is None or len(idx) < len(best):
            best = idx
    return best


def circle_xy(geom, row, n_rows):
    """Where to click to swap `row` (0 = topmost)."""
    lx, rx, sy = geom
    return (lx + rx) // 2, sy - (n_rows - row) * PITCH


def harvest(keep, frame, log=None):
    """Save any glyph that did not read, so the set can be completed by hand."""
    unknown = [c for lb, c in keep if lb is None]
    if not unknown:
        return 0
    try:
        d = os.path.join(ROOT, "ref/auto/ss/balance_unread")
        os.makedirs(d, exist_ok=True)
        t = int(time.time())
        for i, c in enumerate(unknown):
            cv2.imwrite(os.path.join(d, f"glyph_{t}_{i}.png"), c)
        cv2.imwrite(os.path.join(d, f"frame_{t}.png"), frame)
        if log:
            log.info("balance: saved %d unread glyph(s) and the frame to %s",
                     len(unknown), os.path.relpath(d, ROOT))
    except Exception as e:
        if log:
            log.warning("balance: could not save the unread glyphs: %s", e)
    return len(unknown)


# --- playing it ----------------------------------------------------------
#
# A STAGE IS MANY BOARDS UNDER ONE CLOCK, and finding that out cost an
# attempt. Balancing the columns does not end the stage - the game immediately
# deals a FRESH set of numbers with a fresh target, and the timer keeps
# running. Measured across one live stage:
#
#     169s   left 17 18  7 12 = 54   right  5 24 15  2 = 46   target 50
#      94s   left 19  5  9 34 = 67   right 12  1  1 23 = 37   target 52
#
# The first attempt read the second board as the result of its own clicks and
# reported "after the swaps the sums are 67/37, not 50" - a FALSE FAILURE on a
# round it had just won. That is the vacuous-verification trap this project
# already records for the hand-seal slots: a reading taken after the screen
# has moved on says nothing about what we did.
#
# Note the target tracked the total both times (100 -> 50, 104 -> 52), which
# is the strongest evidence for the swap model: the total is only invariant
# under swapping, and a re-roll is exactly where it is allowed to change.
#
# So the round's verdict is "did the board become something OTHER than what my
# swaps would have made it": that is a re-roll, and a re-roll only happens on
# success.
ROUND_CLICK_GAP = 0.35     # between the clicks of one round
REROLL_WAIT = 5.0          # how long a re-roll is waited for
REROLL_POLL = 0.35


def _swapped(rows, pick):
    out = list(rows)
    for i in pick:
        out[i] = (rows[i][1], rows[i][0])
    return out


def play_round(cap, actor, log, verify_swap=False, over=None):
    """Balance ONE board. Returns (verdict, rows).

    verdict is "rerolled" (a new board was dealt, so this one was won),
    "balanced" (the sums reached the target but no new board came), "unread",
    "unsolvable", "gone" (the stage has ended) or "wrong".

    `over(frame)` says whether the stage has finished - the caller supplies it
    because the dialog belongs to `ss`, not here.

    **A BLINK IS NOT AN ENDING.** The first version called the board gone the
    instant `locate` missed, and a re-roll redraws the boxes, so EVERY round
    returned "gone" after zero wins - which meant a wrong answer and a win were
    reported identically. That is worse than a wrong count: it is the loss of
    the only signal that says whether the solver is right. The absence of a
    board now has to persist for the whole wait, and only the stage dialog ends
    a round early.
    """
    f = cap.frame(gray=False)
    keep = []
    board = read_board(f, log, keep)
    if board is None:
        if over is not None and over(f):
            return "gone", None
        if locate(f) is None:
            return "gone", None
        harvest(keep, f, log)
        return "unread", None
    rows, ls, rs, geom = board
    target = (ls + rs) // 2
    pick = solve(rows, ls, rs)
    if pick is None:
        log.info("balance: %s sums %d/%d - no set of swaps reaches %d",
                 rows, ls, rs, target)
        return "unsolvable", None
    log.info("balance: %s = %d/%d, target %d -> swap %s",
             rows, ls, rs, target, pick if pick else "nothing")

    if verify_swap and pick:
        # ONE measured confirmation that a circle swaps its row, kept because
        # the swap model was inferred from the board's arithmetic rather than
        # read off a rules panel. Off by default: it costs an extra board read
        # out of a stage that is on a clock.
        i = pick[0]
        want = (rows[i][1], rows[i][0])
        actor.click_pixel(*circle_xy(geom, i, len(rows)),
                          why=f"balance: swap row {i} {rows[i]} -> {want}")
        time.sleep(REROLL_POLL * 3)
        after = read_board(cap.frame(gray=False), log)
        got = after[0][i] if after and len(after[0]) == len(rows) else None
        log.info("balance: row %d now reads %s (predicted %s) - %s",
                 i, got, want, "swap confirmed" if got == want else "NOT a swap")
        if got != want:
            return "wrong", None
        pick = pick[1:]

    for i in pick:
        actor.click_pixel(*circle_xy(geom, i, len(rows)),
                          why=f"balance: swap row {i}")
        time.sleep(ROUND_CLICK_GAP)

    mine = _swapped(rows, pick)
    last = None
    deadline = time.time() + REROLL_WAIT
    while time.time() < deadline:
        f2 = cap.frame(gray=False)
        if over is not None and over(f2):
            return "gone", rows
        b2 = read_board(f2)
        if b2 is not None:
            last = b2
            if list(b2[0]) != mine:
                log.info("balance: a fresh board was dealt - the round was won")
                return "rerolled", rows
        time.sleep(REROLL_POLL)
    if last is None:
        return "gone", rows
    if last[1] == last[2] == target:
        log.info("balance: balanced at %d/%d, but no new board was dealt",
                 last[1], last[2])
        return "balanced", rows
    log.info("balance: the board still reads %d/%d against a target of %d - "
             "the swaps did not take", last[1], last[2], target)
    return "wrong", None


def play(cap, actor, log, max_rounds=40, over=None):
    """Balance board after board until the stage ends. (won, verdict)."""
    won = 0
    for _ in range(max_rounds):
        verdict, _rows = play_round(cap, actor, log, over=over)
        if verdict in ("rerolled", "balanced"):
            won += 1
            continue
        if verdict == "gone":
            log.info("balance: the board is gone after %d round(s) won", won)
            return won, "gone"
        if verdict == "unread":
            continue                      # a mid-animation frame; look again
        log.info("balance: stopping after %d round(s) won (%s)", won, verdict)
        return won, verdict
    return won, "capped"
