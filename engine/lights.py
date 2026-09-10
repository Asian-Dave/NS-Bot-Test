#!/usr/bin/env python3
"""Lights Out — SS Training's `Sage Sealed Boxes`, and it is ON A CLOCK.

WHAT THE BOARD IS
-----------------
A 3x3 grid of spheres inside a wooden frame, a timer, and nothing else. The
game's own hints panel states the rules, in two languages:

    "There are randomly 1-2 circles with light at the start."
    "Haz clic en un circulo para que la luz ..."
    "Extinguish all the light to clear the stage."

So: a lit sphere is ON, a grey one is OFF, pressing a sphere toggles some
neighbourhood of it, and the goal is all-off. That is Lights Out, and over
GF(2) it is a 9x9 linear system - `A x = b`, where column `j` of `A` is the set
of cells that pressing `j` toggles and `b` is the lit set.

MEASURED GEOMETRY (captured px, the pinned 1720x720 viewport)
-------------------------------------------------------------
    columns   x = 1382, 1733, 2085      pitch 351.5
    rows      y =  668, 1028, 1388      pitch  360
    sphere    274x274, area ~58,800     grey BGR (153,153,153)

**THE BOTTOM ROW IS CLIPPED BY THE VIEWPORT.** The game is 839 CSS px tall in
a 720 px viewport, so its last 238 captured px are hidden, and the bottom row's
spheres are cut off at y=1440: they measure 274x196 instead of 274x274 and
their CENTROIDS read y=1352 instead of 1388. A detector that trusted the
centroid would aim 36 px high on three of the nine cells, and a locator that
demanded three full rows would not find the grid at all. So the two top rows
give the pitch and the third row is derived from it - and the sampling disc is
small enough to stay on screen for the derived row.

This is the same viewport clipping that hides the hints panel's own button; see
`ss.hints_button`.

LEARNING THE TOGGLE RULE INSTEAD OF ASSUMING IT
-----------------------------------------------
The plus shape (the cell and its four orthogonal neighbours) is the standard
rule and is the PRIOR here, not an assumption: every press is read before and
after, so each one reports exactly which cells it toggled, and the model is
corrected from moves that were going to be made anyway. Nothing is spent on
probing - which matters, because this project has already lost an SS mission to
a diagnostic sweep that ate the budget it was diagnosing.

The 3x3 plus-rule matrix is invertible over GF(2), so under the prior every
board has exactly one solution. `solve` does not rely on that: it Gaussian
eliminates and, if the learned matrix turns out to be singular, enumerates the
solution coset and returns the shortest member.
"""
import itertools
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

N = 3                      # the grid is 3x3
CELLS = N * N

# A sphere is either grey or lit cyan. Both are found so the GRID can be
# located from a board in any state - keying only on the lit ones would lose
# the geometry exactly when the puzzle is nearly solved.
GREY = ((0, 0, 90), (180, 60, 230))
CYAN = ((80, 90, 150), (105, 255, 255))
SPHERE_W = (200, 320)
SPHERE_MIN_A = 30000       # full 58,800; a clipped bottom-row sphere 45,100
SPHERE_FULL_H = 250        # a whole sphere measures 274 tall
SPHERE_MIN_H = 150        # a lit one masks 222, a clipped one 196
CLUSTER_TOL = 90           # how close two centres must be to share a row/column

LIT_R = 45                 # sampling disc; small enough to stay on screen for
                           # the derived bottom row (1388 + 45 = 1433 < 1440)
LIT_FRAC = 0.25            # cyan fraction that means ON

PLUS = ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1))

SETTLE = 0.45              # after a press, before reading the board
MAX_PRESSES = 24


def _masks(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    grey = cv2.inRange(hsv, np.array(GREY[0], np.uint8),
                       np.array(GREY[1], np.uint8))
    cyan = cv2.inRange(hsv, np.array(CYAN[0], np.uint8),
                       np.array(CYAN[1], np.uint8))
    return grey, cyan


def _cluster(vals):
    """Sorted cluster means of roughly-equal values."""
    out = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= CLUSTER_TOL:
            out[-1].append(v)
        else:
            out.append([v])
    return [int(round(sum(g) / len(g))) for g in out]


def locate(frame):
    """(xs, ys) - three column and three row centres - or None.

    THE TOP-LEFT CORNER OF A BLOB IS THE RELIABLE PART. Two of the nine cells
    are drawn in ways that shrink or cut their mask, and the centroid is wrong
    for both:

        a LIT sphere      masks 232x222 against a grey one's 274x274 - its
                          bright core falls outside both colour ranges, so the
                          centroid lands 24 px up and left of the true centre
        the BOTTOM ROW    is clipped by the viewport at y=1440, masking 274x196,
                          so its centroid sits 36 px high

    In both cases the LEFT and TOP edges are intact and only the far side is
    missing, so `left + R` and `top + R` recover the true centre, with R taken
    from the whole spheres present on this very frame rather than assumed.
    Measured on two live boards, that reconstructs all nine centres to within
    7 px - against a sphere radius of 137.

    Requiring six whole spheres does NOT work: with one light on, only five
    are whole, and two x clusters cannot say WHICH two of the three columns
    they are. Reconstructing the centres first removes the ambiguity instead
    of guessing at it.
    """
    grey, cyan = _masks(frame)
    m = cv2.morphologyEx(grey | cyan, cv2.MORPH_CLOSE,
                         np.ones((11, 11), np.uint8))
    n, _lab, st, _ce = cv2.connectedComponentsWithStats(m)
    blobs = []
    for i in range(1, n):
        bw = int(st[i, cv2.CC_STAT_WIDTH])
        bh = int(st[i, cv2.CC_STAT_HEIGHT])
        a = int(st[i, cv2.CC_STAT_AREA])
        if a < SPHERE_MIN_A or not (SPHERE_W[0] <= bw <= SPHERE_W[1]):
            continue
        if not (SPHERE_MIN_H <= bh <= SPHERE_W[1]):
            continue
        blobs.append((int(st[i, cv2.CC_STAT_LEFT]),
                      int(st[i, cv2.CC_STAT_TOP]), bw, bh))
    if len(blobs) < N:
        return None
    whole = [b for b in blobs if b[3] >= SPHERE_FULL_H]
    if not whole:
        return None
    r = int(round(float(np.median([b[2] for b in whole])) / 2))
    xs = _cluster([b[0] + r for b in blobs])
    ys = _cluster([b[1] + r for b in blobs])
    if len(xs) != N or len(ys) < 2 or len(ys) > N:
        return None
    if len(ys) == 2:
        ys = ys + [ys[1] + (ys[1] - ys[0])]
    return xs, ys


def cell_xy(geom, i):
    xs, ys = geom
    return xs[i % N], ys[i // N]


def state(frame, geom):
    """A 9-tuple of bools, True where a sphere is LIT."""
    _grey, cyan = _masks(frame)
    h, w = cyan.shape[:2]
    out = []
    for i in range(CELLS):
        cx, cy = cell_xy(geom, i)
        x0, x1 = max(0, cx - LIT_R), min(w, cx + LIT_R)
        y0, y1 = max(0, cy - LIT_R), min(h, cy + LIT_R)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        sub = cyan[y0:y1, x0:x1]
        out.append(bool(sub.mean() / 255.0 >= LIT_FRAC))
    return tuple(out)


# --- the linear algebra ---------------------------------------------------
def matrix(rule=PLUS):
    """columns[j] = bitmask of the cells pressing j toggles."""
    cols = []
    for j in range(CELLS):
        r, c = divmod(j, N)
        bits = 0
        for dr, dc in rule:
            rr, cc = r + dr, c + dc
            if 0 <= rr < N and 0 <= cc < N:
                bits |= 1 << (rr * N + cc)
        cols.append(bits)
    return cols


def solve(lit, cols):
    """The shortest set of cells to press, or None if unreachable.

    Gaussian elimination over GF(2) on the 9x9 system, then - because a
    LEARNED matrix has no guarantee of being invertible, unlike the plus
    rule's - the free variables are enumerated and the shortest solution
    returned. At most 2^k with k the nullity, and k is 0 under the prior.
    """
    b = 0
    for i, v in enumerate(lit):
        if v:
            b |= 1 << i
    # rows[i] = (coefficients over the 9 unknowns, rhs bit)
    rows = []
    for i in range(CELLS):
        coef = 0
        for j in range(CELLS):
            if cols[j] >> i & 1:
                coef |= 1 << j
        rows.append((coef, b >> i & 1))
    pivots = {}
    for j in range(CELLS):
        pick = next((k for k in range(len(rows))
                     if k not in pivots.values() and rows[k][0] >> j & 1), None)
        if pick is None:
            continue
        for k in range(len(rows)):
            if k != pick and rows[k][0] >> j & 1:
                rows[k] = (rows[k][0] ^ rows[pick][0], rows[k][1] ^ rows[pick][1])
        pivots[j] = pick
    # A row with no coefficients and rhs 1 is a contradiction.
    for coef, rhs in rows:
        if coef == 0 and rhs:
            return None
    free = [j for j in range(CELLS) if j not in pivots]
    best = None
    for combo in itertools.product((0, 1), repeat=len(free)):
        x = 0
        for j, v in zip(free, combo):
            if v:
                x |= 1 << j
        for j in sorted(pivots, reverse=True):
            coef, rhs = rows[pivots[j]]
            v = rhs
            for k in range(CELLS):
                if k != j and coef >> k & 1 and x >> k & 1:
                    v ^= 1
            if v:
                x |= 1 << j
            else:
                x &= ~(1 << j)
        # verify, because a learned matrix may be inconsistent
        acc = 0
        for j in range(CELLS):
            if x >> j & 1:
                acc ^= cols[j]
        if acc != b:
            continue
        moves = [j for j in range(CELLS) if x >> j & 1]
        if best is None or len(moves) < len(best):
            best = moves
    return best


def learn(cols, pressed, before, after):
    """Fold an observed press into the model. True if it changed anything."""
    seen = 0
    for i in range(CELLS):
        if before[i] != after[i]:
            seen |= 1 << i
    if seen == cols[pressed] or seen == 0:
        return False
    cols[pressed] = seen
    return True


def play(cap, actor, log, over=None):
    """Extinguish the board. (presses, verdict).

    verdict is "cleared" (the board went dark or the stage ended), "gone" (no
    board on screen), "unreachable" (the model says this state cannot be
    reached), or "stuck".
    """
    f = cap.frame(gray=False)
    geom = locate(f)
    if geom is None:
        log.info("lights: no 3x3 board on screen")
        return 0, "gone"
    cols = matrix(PLUS)
    log.info("lights: board at columns %s rows %s", *geom)
    presses = 0
    for _ in range(MAX_PRESSES):
        f = cap.frame(gray=False)
        if over is not None and over(f):
            log.info("lights: the stage ended after %d press(es)", presses)
            return presses, "cleared"
        g2 = locate(f)
        if g2 is None:
            log.info("lights: the board is gone after %d press(es)", presses)
            return presses, "cleared" if presses else "gone"
        geom = g2
        lit = state(f, geom)
        if lit is None:
            return presses, "gone"
        if not any(lit):
            log.info("lights: every light is out after %d press(es)", presses)
            return presses, "cleared"
        moves = solve(lit, cols)
        if not moves:
            log.info("lights: %s cannot be solved under the learned rule",
                     _grid(lit))
            return presses, "unreachable"
        log.info("lights: %s -> press %s", _grid(lit), moves)
        j = moves[0]
        actor.click_pixel(*cell_xy(geom, j), why=f"lights: press cell {j}")
        time.sleep(SETTLE)
        presses += 1
        f2 = cap.frame(gray=False)
        if over is not None and over(f2):
            return presses, "cleared"
        g3 = locate(f2)
        if g3 is None:
            return presses, "cleared"
        after = state(f2, g3)
        if after is None:
            continue
        # EVERY PRESS IS EVIDENCE, and it is free - the move was going to be
        # made anyway. Only the toggle set actually observed is recorded.
        if after != lit and learn(cols, j, lit, after):
            log.info("lights: pressing %d toggled %s, not the plus shape - "
                     "model corrected", j, _grid([after[k] != lit[k]
                                                  for k in range(CELLS)]))
    return presses, "stuck"


def _grid(bits):
    return "".join("O" if b else "." for b in bits)
