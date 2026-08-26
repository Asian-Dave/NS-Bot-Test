#!/usr/bin/env python3
"""Memory pair-matching minigame (the TP "Scroll" family).

WHAT IT IS
----------
Seen live in "Secret TP Scroll". A 4x5 grid of 20 face-down cards, an hourglass
counting down (~84 at the start), and a "Remaining Cards: x20" label. Ten pairs.
Flip two at a time; a match clears them, a miss flips them back.

There is NO opening reveal. A 26 fps burst over 8 s of the whole grid caught zero
change, so the whole grid stays face-down until you start clicking - this is a
genuine memory game, not a look-then-recall one.

**IT IS TIMED, AND THE TIMER IS THE REAL OPPONENT.** Running the clock out ends
the mission with "Sorry, you are not qualified to receive this scroll." Every
design decision below is either about reading the board CORRECTLY or about
reading it FAST; nothing else matters here.

THE CARD BACKS ARE ANIMATED, AND THAT BROKE THE FIRST TWO ATTEMPTS
-----------------------------------------------------------------
The back art is the Ninja Saga logo over FLAMES that animate, so at any instant
every back looks different. Measured on a frame where all twenty cards were
visually face-down, the spatial distance from cell 0 to the others ran
0 .. 127.6 - completely overlapping real face-to-face distances.

So "is this card face-down?" CANNOT be a distance to a back exemplar. What does
separate them is the AGGREGATE, because the animation moves the spatial pattern
but hardly moves the mean:

    animated backs   sat 28.2 .. 30.1   val 123.5 .. 125.0
    card faces       sat 113.4 .. 201.9 val  96.9 .. 185.1

Mean saturation alone gives a gap of 83, so `cell_state` gates on it.

FACE MATCHING — MEASURED, AND NOT THE REFERENCE BOT'S METRIC
------------------------------------------------------------
`ref/tp/cmmhero`'s `CardSolver.cs` compares crops with a dual metric: inset 20%,
resize 70x70, greyscale, then `mean(|grey diff|) + mean(|Canny diff|)`. That was
ported first and it is **not good enough for this board**. Calibrated against a
full set of twenty real face crops whose ten true pairs are known
(`ref/auto/tp/faces/`):

    metric                     worst true pair   best NON-pair   gap
    dual grey+Canny (theirs)            139.85          104.96   INVERTED
    mean saturation+value                 3.39            1.50   INVERTED
    3x4 mean-HSV signature (ours)         4.56            8.99   1.97x

The dual metric's true-pair distances *overlap* its non-pair distances, which is
exactly why the earlier threshold could not be tuned and why mutual-best had to
carry the whole decision. A **coarse mean-HSV signature separates cleanly**: the
two halves of a pair are the same artwork, so their colour layout agrees to a few
units, while different artwork disagrees by more than twice that.

Inset is actively harmful here (3x4 with a 20% inset drops the gap to 1.27x), so
the crop is used whole. Coarser is not automatically better either - 2x3 scores a
wider 2.68x on this one board, but with only six cells two differently-drawn
cards that happen to share a colour balance would collide, so 3x4 keeps some
spatial structure at a gap that is still comfortable.

TRUST THE GAME, NOT THE METRIC
------------------------------
A previous run reported "10/10 pairs" while the game's own "Remaining Cards: x18"
showed a single real match. The metric was believed; the board was not consulted.
It is now the other way round: after a pair is clicked, `_settle_pair` WATCHES
the two cells. If they go blank the match was real; if they flip back to backs it
was wrong, and that pair is recorded in `rejected` so it is never tried again
while both faces stay in memory. The game adjudicates, the metric only proposes.

SPEED: EVENT-DRIVEN, CLIPPED, AND UNPACED
-----------------------------------------
Three separate costs were making a 40-flip game miss an ~84 s timer:

1. **Full-frame capture.** 3440x1440 costs ~165 ms. Only the board matters, so
   every read is a server-side clip of `BOARD_BOX`, which also carries the HUD
   anchor so presence and cell reads come from ONE capture.
   NOTE the clip is taken at `scale=dpr`: CDP's own default of 1 returns CSS
   resolution, which on this Retina host is half size, and that silently broke
   the first attempt at this - every cell read the wrong pixels.
2. **Fixed sleeps.** `flip_settle=0.75` was a guess applied to every flip. Reads
   now POLL the cell until it actually becomes a face (or the deadline passes),
   so a fast flip costs one capture instead of 750 ms.
3. **Human-like click pacing.** `Actor`'s defaults sleep 0.18-0.55 s before and
   0.4-1.1 s after EVERY click - up to 1.65 s each, ~40 s over a game, on a
   board with a countdown. `_fast_actor` tightens them for the duration and puts
   them back afterwards. Pacing is anti-detection cosmetics; here it is the
   difference between finishing and failing.

EVERY CLICK IS GATED ON THE BOARD STILL BEING THERE
---------------------------------------------------
The first version of this file clicked the twenty fixed grid coordinates in a
loop and verified NOTHING. The minigame ends partway through a run - the timer
expires - and it carried on clicking the same coordinates over whatever screen
had replaced it. Those coordinates walked into the village, into the weapon Shop,
and onto a purchase confirmation for a kunai. Nothing was bought, but only
because Buy happens to sit away from the grid; that is luck, not safety.

So `board_present()` checks the "Remaining Cards:" HUD anchor (1.000 on a card
frame, 0.396 worst negative) and EVERY click goes through a fresh board read that
aborts the run when the anchor is gone. Fixed coordinates are only ever safe
behind a live check that the thing you measured them against is still on screen.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

# --- measured geometry, CAPTURED px at the standard 1720x720 viewport --------
COLS = [1434, 1572, 1712, 1850, 1991]
ROWS = [483, 669, 852, 1038]
CARD_W, CARD_H = 100, 140            # crop half-extents are CARD_W/2, CARD_H/2
START_XY = (1724, 769)
N = len(ROWS) * len(COLS)            # 20

# The union of the grid (x 1384..2041, y 413..1108) and the "Remaining Cards:"
# HUD anchor (centre 1070,260, 400x56), with margin. One clip serves both, so a
# board read costs a single capture of ~24% of the full frame.
BOARD_BOX = (856, 216, 1200, 904)    # x, y, w, h in captured px

# Face-vs-back, from the measured bands above. Anywhere in 40..100 works.
SAT_GATE = 70.0
# A cleared cell is a blank slot: no colour, and much brighter than a card back
# (backs sit at val 123.5..125.0).
REMOVED_VAL = 190.0

# Signature match gate, set between the measured worst true pair (4.56) and best
# non-pair (8.99) on the twenty-crop calibration board.
MATCH_GATE = 6.5

_HUD = None

FACE, BACK, REMOVED = "face", "back", "removed"


def pos_xy(i):
    """Card index 0..19 -> (x, y) in CAPTURED px. Row-major, left to right."""
    return COLS[i % len(COLS)], ROWS[i // len(COLS)]


def crop(frame, i, origin=(0, 0)):
    """Cell i out of a frame whose top-left is `origin` in captured px.

    `origin` is what makes the same geometry work on a full frame (0,0) and on a
    BOARD_BOX clip - the clip is captured at scale=dpr so it is the same pixel
    space, just translated.
    """
    x, y = pos_xy(i)
    x -= origin[0]
    y -= origin[1]
    return frame[max(0, y - CARD_H // 2):y + CARD_H // 2,
                 max(0, x - CARD_W // 2):x + CARD_W // 2]


def board_frame(cap):
    """One board read: (image, origin). Cheap - clipped server-side."""
    x, y, w, h = BOARD_BOX
    clip, origin = cap.clip_for(x, y, w, h)
    return cap.frame(gray=False, clip=clip), origin


def board_present(frame, origin=(0, 0)):
    """Is the card board actually on screen? Gate every click on this.

    Uses the "Remaining Cards:" label rather than the cells, because a cell can
    legitimately look like anything (back, face, mid-flip, cleared) while the
    board is up, whereas this label is present for the whole game and absent
    everywhere else.
    """
    global _HUD
    if _HUD is None:
        from perceive import Template
        p = os.path.join(ROOT, "tpl", "tp_cards_hud.png")
        if not os.path.exists(p):
            return None                 # unknown, not "yes"
        _HUD = Template("tp_cards_hud", p, threshold=0.88)
    from perceive import find
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if _HUD.h > g.shape[0] or _HUD.w > g.shape[1]:
        return None
    return find(g, _HUD)[0].found


def sig(c):
    """3x4 mean-HSV signature — the face identity used for matching.

    Deliberately coarse and NOT inset: see the module docstring for the measured
    comparison against the reference bot's grey+Canny metric, which does not
    separate this board at all.
    """
    if c is None or c.size == 0:
        return None
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV).astype(np.float32)
    return cv2.resize(hsv, (3, 4), interpolation=cv2.INTER_AREA).reshape(-1)


def distance(a, b):
    if a is None or b is None:
        return 1e9
    return float(np.abs(a - b).mean())


def cell_state(frame, i, origin=(0, 0)):
    """FACE, BACK or REMOVED for cell i."""
    c = crop(frame, i, origin)
    if c is None or c.size == 0:
        return BACK
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1].mean(), hsv[:, :, 2].mean()
    if s >= SAT_GATE:
        return FACE
    return REMOVED if v >= REMOVED_VAL else BACK


class Board:
    """What we know, and what the GAME has told us is wrong.

    Matching is proposed by mutual-best-partner AND a calibrated gate, then
    adjudicated by the board itself:

    * mutual-best alone is vacuous early on - with two revealed cards each is
      trivially the other's best, which is how a previous run "matched" ten pairs
      that were not pairs. `MATCH_GATE` kills that.
    * the gate alone is a threshold, and a threshold on one board is a guess on
      the next. Mutual-best keeps it honest.
    * `rejected` records every pair the game refused, so a wrong proposal is made
      at most once and the two faces stay in memory for their real partners.
    """

    def __init__(self, log, sat_gate=SAT_GATE, match_gate=MATCH_GATE):
        self.log = log
        self.seen = {}                  # pos -> signature
        self.cleared = set()            # the GAME removed these
        self.skipped = set()            # would not flip; taken out of rotation
        self.rejected = set()           # frozenset({i, j}) the game refused
        self.sat_gate = sat_gate
        self.match_gate = match_gate

    def identify(self, frame, i, origin=(0, 0)):
        """Record the face at position i. True if a face was actually there."""
        if cell_state(frame, i, origin) != FACE:
            return None
        s = sig(crop(frame, i, origin))
        if s is None:
            return None
        self.seen[i] = s
        return True

    def _live(self):
        return [p for p in self.seen if p not in self.cleared]

    def _ok(self, i, j):
        return frozenset((i, j)) not in self.rejected

    def best_partner(self, i):
        """Mutual-best partner of i, inside the gate and not already refused."""
        live = [p for p in self._live() if p != i and self._ok(i, p)]
        if not live:
            return None
        j = min(live, key=lambda p: distance(self.seen[i], self.seen[p]))
        if distance(self.seen[i], self.seen[j]) > self.match_gate:
            return None
        back = [p for p in self._live() if p != j and self._ok(j, p)]
        if not back:
            return None
        k = min(back, key=lambda p: distance(self.seen[j], self.seen[p]))
        return j if k == i else None

    def pending_pair(self):
        """The most confident proposable pair among revealed positions."""
        best, bd = None, 1e9
        for i in self._live():
            j = self.best_partner(i)
            if j is None:
                continue
            d = distance(self.seen[i], self.seen[j])
            if d < bd:
                best, bd = (i, j), d
        return best

    def unknown_positions(self):
        """Cells still worth flipping.

        `skipped` MUST be excluded alongside `cleared`. Leaving it out is a
        livelock: a cell that will not flip stays "unknown", `unk[0]` picks it
        again on the very next pass, and the run spends its whole clock clicking
        two cells. That happened live - 831 reads, 80 s, the same two cards.
        """
        return [i for i in range(N) if i not in self.cleared
                and i not in self.skipped and i not in self.seen]


class _FastActor:
    """Tighten click pacing for the duration of a timed minigame, then restore.

    `Actor`'s defaults sleep up to 1.65 s per click. Across ~40 flips that is
    most of the game's clock. Anti-robotic pacing is worth having in the village;
    on a countdown it is just a way to lose.
    """

    def __init__(self, actor, click=(0.02, 0.05), post=(0.02, 0.05)):
        self.a, self.click, self.post = actor, click, post

    def __enter__(self):
        self._save = (self.a.click_delay, self.a.post_click)
        self.a.click_delay, self.a.post_click = self.click, self.post
        return self.a

    def __exit__(self, *exc):
        self.a.click_delay, self.a.post_click = self._save
        return False


# How long to pause between board reads. Capture alone runs at ~20 fps, and
# polling that hard makes the game visibly FLICKER, because every
# Page.captureScreenshot forces the WebGL canvas to re-composite. The user
# watching the screen is a real constraint, and there is nothing to spend the
# extra frames on: a solved board finished in 33.9 s of an 85 s clock, so ~8
# reads/second is both comfortable to look at and far more than fast enough.
POLL_INTERVAL = 0.10

# A matched pair does NOT vanish instantly - it burns away in a smoke puff, and
# mid-puff both cells read as backs. Judging a pair on a snapshot therefore
# reports "not a pair" for pairs that actually matched: a live run called 14 of
# them wrong while clearing the whole board. REMOVED is terminal and is trusted
# at once; BACK has to HOLD for this long before it counts as a mismatch.
MISMATCH_HOLD = 1.2


def solve_live(cap, actor, log, timeout=80.0, save_crops=False,
               poll_interval=POLL_INTERVAL):
    """Play the board. Returns (cleared_pairs, elapsed).

    Standard optimal-memory strategy:
      1. if two revealed cards are a proposable pair, click it
      2. else reveal an unknown card; if its partner is already revealed, click
         that partner immediately
      3. else reveal a second unknown and remember both

    Every outcome is confirmed against the cells themselves, so the returned
    count is what the GAME cleared, not what the metric hoped for.
    """
    frame, origin = board_frame(cap)
    if board_present(frame, origin) is False:
        # Start has not been pressed yet, or this is not the board at all. The
        # HUD is present the whole game, so check the full frame once before
        # giving up - a clip cannot see a Start button drawn outside BOARD_BOX.
        full = cap.frame(gray=False)
        if board_present(full) is False:
            log.info("card board is not on screen - refusing to click a grid of "
                     "fixed coordinates at nothing")
            return 0, 0.0
        frame, origin = full, (0, 0)

    # The board is up before Start is pressed but the cards are inert until it
    # is. Located by template (margin 0.774) rather than by its coordinate, so a
    # shifted layout cannot send this click somewhere unintended.
    from perceive import Template as _T, find as _f
    sp = os.path.join(ROOT, "tpl", "tp_cards_start.png")
    if os.path.exists(sp):
        full = cap.frame(gray=False)
        sm, sc = _f(cv2.cvtColor(full, cv2.COLOR_BGR2GRAY), _T("start", sp, 0.88))
        if sm.found:
            log.info("pressing Start (%.3f)", sc)
            actor.click_pixel(*sm.center, why="Start the memory board")
            time.sleep(1.2)

    b = Board(log)
    t0 = time.time()
    aborted = {"why": None}
    reads = {"n": 0, "t": 0.0}

    def read(pace=True):
        """One board capture, with the abort check folded in.

        Paced on purpose - see POLL_INTERVAL. Unpaced polling is both harder to
        watch and pointless here.
        """
        if pace and poll_interval:
            time.sleep(poll_interval)
        s = time.time()
        f, o = board_frame(cap)
        reads["n"] += 1
        reads["t"] += time.time() - s
        if board_present(f, o) is False:
            aborted["why"] = "board disappeared"
            return None, o
        return f, o

    def flip(i, deadline=1.4):
        """Click cell i and POLL until it shows a face. Returns (ok, frame, origin).

        Polling instead of sleeping is most of the speed win: a flip that lands
        in 150 ms costs one capture, not a 750 ms guess. The deadline is a
        backstop, not the normal path.
        """
        f, o = read(pace=False)
        if f is None:
            log.info("board gone before flipping card %d - aborting instead of "
                     "clicking through to whatever replaced it", i)
            return None, None, o
        if cell_state(f, i, o) == REMOVED:
            # SELF-HEALING. The game already took this cell, whatever we thought.
            # This is the backstop for a pair we wrongly recorded as refused:
            # a mistake there costs one wasted click, not a poisoned board.
            log.info("card %d is already gone - the board had taken it", i)
            b.cleared.add(i)
            b.seen.pop(i, None)
            return False, f, o
        actor.click_pixel(*pos_xy(i), why=f"flip card {i}")
        t = time.time()
        while time.time() - t < deadline:
            f, o = read()
            if f is None:
                log.info("board gone right after flipping card %d - stopping", i)
                return None, None, o
            if cell_state(f, i, o) == FACE:
                if save_crops:
                    # NOT ref/auto/tp/faces - that holds the twenty COMMITTED
                    # calibration crops whose true pairing the match-gate test
                    # asserts against. A live run dumping hundreds of captures
                    # into it would destroy the fixture.
                    d = os.path.join(ROOT, "ref/auto/tp/faces_live")
                    os.makedirs(d, exist_ok=True)
                    cv2.imwrite(os.path.join(
                        d, f"pos{i:02d}_{int(time.time()*1000)}.png"),
                        crop(f, i, o))
                # RECORDING THE FACE IS THE POINT OF THE FLIP. Returning "yes it
                # is a face" without storing the signature left `seen` empty, so
                # `unknown_positions()` never shrank and the run re-flipped the
                # same two cells for its entire 80 s clock.
                b.identify(f, i, o)
                return True, f, o
        return False, f, o

    def settle_pair(i, j, deadline=4.0):
        """Let the GAME judge the pair. True only if both cells actually clear.

        This is the correctness fix. A metric that says "match" and a board that
        says otherwise disagree often enough that a previous run banked ten
        imaginary pairs against a real score of one.

        It doubles as the inter-move barrier: it does not return until the two
        cells have resolved, so the next click never lands while the board is
        still animating (which the game ignores).

        THE VERDICT IS ASYMMETRIC, AND THAT IS THE WHOLE POINT
        ------------------------------------------------------
        A matched pair burns away in a SMOKE PUFF, and mid-puff the cells read
        as backs - the same reading a mismatch gives. Treating the first
        BACK/BACK as a mismatch called 14 pairs wrong in a run that nonetheless
        cleared the entire board ("Remaining Cards: x0" with 51 s to spare).

        So: REMOVED is terminal and is believed immediately; BACK has to persist
        for MISMATCH_HOLD before it means anything. A real mismatch pays that
        wait once; a real match does not pay it at all.
        """
        t = time.time()
        back_since = None
        while time.time() - t < deadline:
            f, o = read()
            if f is None:
                return None
            si, sj = cell_state(f, i, o), cell_state(f, j, o)
            if si == REMOVED and sj == REMOVED:
                return True
            if si == BACK and sj == BACK:
                if back_since is None:
                    back_since = time.time()
                elif time.time() - back_since >= MISMATCH_HOLD:
                    return False
            else:
                back_since = None       # still animating; start the hold over
        log.info("  pair %d,%d never resolved within %.1fs", i, j, deadline)
        return False

    def try_pair(i, j, how):
        """Click j (i is already face-up) and record the verdict."""
        d = distance(b.seen[i], b.seen[j]) if i in b.seen and j in b.seen else -1
        log.info("%s: %d,%d (d=%.2f)", how, i, j, d)
        ok, _, _ = flip(j)
        if ok is None:
            return None
        if not ok:
            if j not in b.cleared:      # `flip` banks an already-gone cell itself
                log.info("  card %d would not flip; taking it out of rotation", j)
                b.skipped.add(j)
            return False
        got = settle_pair(i, j)
        if got is None:
            return None
        if got:
            b.cleared.update((i, j))
            log.info("  MATCH confirmed by the board (%d cleared)", len(b.cleared))
        else:
            b.rejected.add(frozenset((i, j)))
            log.info("  the board refused it - remembering both faces, and never "
                     "proposing this pair again")
        return got

    while (time.time() - t0 < timeout and len(b.cleared) < N
           and aborted["why"] is None):
        pair = b.pending_pair()
        if pair:
            a, c = pair
            ok, _, _ = flip(a)
            if ok is None:
                break
            if try_pair(a, c, "known pair") is None:
                break
            continue

        unk = b.unknown_positions()
        if not unk:
            log.info("nothing unknown left and no proposable pair; stopping")
            break

        a = unk[0]
        fa, _, _ = flip(a)
        if fa is None:
            break
        if not fa:
            if a not in b.cleared:      # `flip` banks an already-gone cell itself
                # NOT `cleared` - that would inflate the score with cells the
                # game never removed. Out of rotation, counted as what it is.
                log.info("card %d would not flip; taking it out of rotation", a)
                b.skipped.add(a)
            continue

        partner = b.best_partner(a)
        if partner is not None:
            if try_pair(a, partner, "partner already revealed") is None:
                break
            continue

        unk2 = [u for u in b.unknown_positions() if u != a]
        if not unk2:
            # Nothing new to turn over, and no proposable pair among what is
            # revealed. Flipping `a` back and forth achieves nothing.
            log.info("card %d is up but there is nothing left to pair it with")
            break
        c = unk2[0]
        fc, _, _ = flip(c)
        if fc is None:
            break
        if not fc:
            if c not in b.cleared:
                log.info("card %d would not flip; taking it out of rotation", c)
                b.skipped.add(c)
            continue

        # BOTH ARE FACE-UP, SO THE GAME IS ABOUT TO RULE ON THEM ANYWAY. Ask it,
        # instead of asking the metric: `settle_pair` watches the two cells and
        # reports what actually happened. A free, authoritative label on every
        # exploratory move - and it is also the barrier that stops the next click
        # landing mid-resolution, which the game silently discards.
        verdict = settle_pair(a, c)
        if verdict is None:
            break
        if verdict:
            b.cleared.update((a, c))
            log.info("cards %d,%d matched on the turn-over (%d cleared)",
                     a, c, len(b.cleared))
        else:
            b.rejected.add(frozenset((a, c)))
            log.info("cards %d,%d are not a pair - both faces remembered", a, c)

    el = time.time() - t0
    log.info("memory game: %d/%d cells cleared BY THE GAME, %d faces known, "
             "%d pair(s) refused, %d skipped, %.1fs (%d reads, %.0f ms each)%s",
             len(b.cleared), N, len(b.seen), len(b.rejected), len(b.skipped),
             el, reads["n"], 1000 * reads["t"] / max(1, reads["n"]),
             f" (ABORTED: {aborted['why']})" if aborted["why"] else "")
    return len(b.cleared) // 2, el


def play(cap, actor, log, timeout=80.0, save_crops=False,
         poll_interval=POLL_INTERVAL):
    """solve_live with the minigame's click pacing - what callers should use."""
    with _FastActor(actor) as fast:
        return solve_live(cap, fast, log, timeout=timeout,
                          save_crops=save_crops, poll_interval=poll_interval)


def main():
    import argparse
    from act import Actor
    from capture import Capture
    from cdp import CDP, find_page_target

    class _Log:
        def info(self, m, *a):
            print(("  " + m) % a if a else "  " + m, flush=True)
        warning = error = info

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--timeout", type=float, default=80.0)
    ap.add_argument("--save-crops", action="store_true",
                    help="write every revealed face to ref/auto/tp/faces/")
    ap.add_argument("--probe", action="store_true",
                    help="measure capture cost and read the board once, WITHOUT "
                         "clicking anything")
    a = ap.parse_args()
    log = _Log()

    t = find_page_target(port=a.port, url_contains="ninjasaga", timeout=20)
    c = CDP(t["webSocketDebuggerUrl"])
    c.call("Page.enable")
    cap = Capture(c)

    if a.probe:
        import statistics
        for label, fn in (("full frame", lambda: cap.frame(gray=False)),
                          ("board clip", lambda: board_frame(cap)[0])):
            ts = []
            for _ in range(6):
                s = time.time()
                fn()
                ts.append((time.time() - s) * 1000)
            print(f"  {label:12s} {statistics.median(ts):6.1f} ms  "
                  f"({1000/statistics.median(ts):.1f} fps)")
        f, o = board_frame(cap)
        print(f"  clip shape {f.shape}, origin {o}, "
              f"HUD present={board_present(f, o)}")
        for i in range(N):
            cc = crop(f, i, o)
            hsv = cv2.cvtColor(cc, cv2.COLOR_BGR2HSV)
            print(f"    cell {i:2d} {str(cc.shape[:2]):10s} sat={hsv[:,:,1].mean():6.1f}"
                  f" val={hsv[:,:,2].mean():6.1f} -> {cell_state(f, i, o)}")
        c.close()
        return 0

    actor = Actor(c, cap, log, dry_run=False)
    pairs, el = play(cap, actor, log, timeout=a.timeout, save_crops=a.save_crops)
    print(f"\ncleared {pairs} pair(s) in {el:.1f}s")
    c.close()
    return 0 if pairs else 1


if __name__ == "__main__":
    sys.exit(main())
