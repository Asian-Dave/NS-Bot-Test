#!/usr/bin/env python3
"""SS Training — the next tier of dailies, and the multi-stage rune puzzle.

WHAT SS IS
----------
Mission Room -> `Special` tab -> `SS Training`, below TP Training. Five
missions, all Lv 80, XP 10,000, Gold 10,000, flame 30 — five times TP's reward
against TP's flame of 10:

    Twins Unicorn · Sage Power Seal · Forest Guardians
    Balance Control · Sage Sealed Boxes

Only `Sage Power Seal` has been opened so far. It is **the same rune Mastermind
as the TP kekkai**, and the game's own rules panel states the feedback mapping
this project previously had to establish by carrying two hypotheses through
live play:

    green disc   "each rune from the guess which is correct in both pattern
                  and position"
    gold disc    "a correct rune pattern placed in the wrong position"

MULTI-STAGE, AND THE LENGTH ESCALATES
-------------------------------------
One mission is several stages, each its own puzzle with its own fresh scroll of
ten rows. Observed: stage 1 had FIVE nodes and stage 2 had SIX. So the code
length must be re-read per stage, never carried over — `stage_length` counts
the seal's nodes on the open panel.

**TEN ROWS IS THE BUDGET PER STAGE**, and running out fails the whole mission,
not just the stage. That was learned the expensive way: a diagnostic sweep of
six all-same probes left only four rows for solving and the mission failed with
`Mission Fail — Sorry, try again next time`. Knuth selection needs roughly six
to eight guesses at length six, which fits ten; a probe sweep does not.

What the two stages also showed is that the secret's SHAPE varies:

    stage 1 (five)   the answer REPEATED a rune (White twice)
    stage 2 (six)    the answer was a PERMUTATION of all six

so the solver must keep allowing repeats. Assuming otherwise would have
eliminated stage 1's true answer outright.

WHAT THIS MODULE DOES NOT DO
----------------------------
It plays the rune puzzle only. The other four SS missions have never been
opened, and this file does not guess at them: `minigame.classify` reads the
family off the screen, and anything it cannot name is left alone rather than
clicked at.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import kekkai_play as K  # noqa: E402
import tp  # noqa: E402
from perceive import find  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# SS Training on the Special tab. The entries are evenly pitched and TP
# Training - which the bot already clicks reliably at (2121, 862) - is directly
# above it.
SS_ROW_XY = (2118, 1037)

# The stage dialog's OK button, found by COLOUR. Measured on live frames:
#
#     Stage Clear    a GREEN button, area 31542, 351x108, centre (1712,  991)
#     Mission Fail   a RED   button, area 29891, 352x108, centre (1720, 1015)
#     the puzzle      neither
#
# Colour is the discriminator because the two dialogs are otherwise the same
# shape in the same place, and the difference decides whether to carry on to
# the next stage or stop.
DIALOG_BAND = (1400, 850, 2100, 1150)      # x0, y0, x1, y1
DIALOG_GREEN = ((35, 120, 90), (85, 255, 255))
DIALOG_RED = ((0, 140, 90), (10, 255, 255))
DIALOG_MIN_AREA = 2500
DIALOG_ASPECT = (1.5, 5.0)                 # a wide button, not a disc

# The seal sits in the middle of the open panel and its nodes ARE the code
# length. The default `count_nodes` box is cut for the TP triangle and clips a
# pentagon or hexagon - measured, it returned 3 for a five-node seal.
NODE_BOX = (320, 300)


def _button(frame, lo, hi):
    x0, y0, x1, y1 = DIALOG_BAND
    h, w = frame.shape[:2]
    x0, x1 = max(0, min(x0, w)), max(0, min(x1, w))
    y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, _, st, ce = cv2.connectedComponentsWithStats(m)
    best = None
    for i in range(1, n):
        a = st[i, cv2.CC_STAT_AREA]
        bw, bh = st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
        if a < DIALOG_MIN_AREA or bh == 0:
            continue
        if not (DIALOG_ASPECT[0] <= bw / bh <= DIALOG_ASPECT[1]):
            continue
        if best is None or a > best[0]:
            best = (a, int(ce[i][0]) + x0, int(ce[i][1]) + y0)
    return (best[1], best[2]) if best else None


def stage_dialog(frame):
    """("clear"|"fail", (x, y)) for the stage dialog's OK button, or None."""
    g = _button(frame, *DIALOG_GREEN)
    if g:
        return "clear", g
    r = _button(frame, *DIALOG_RED)
    if r:
        return "fail", r
    return None


def stage_length(frame, log=None):
    """The open stage's code length, from the seal's node count. None if unsure.

    Read PER STAGE. The length escalates between stages of one mission - five
    nodes then six - so a length carried over from the previous stage would be
    wrong, and this project has already paid once for solving a five-node seal
    as a three-rune code.
    """
    centre = K.find_confirm_point(frame)
    if centre is None:
        return None
    n = K.count_nodes(frame, centre, box=NODE_BOX)
    if n and 2 <= n <= 6:
        if log:
            log.info("stage: %d nodes at %s -> code length %d", n, centre, n)
        return n
    if log:
        log.info("stage: node count %s at %s is not a plausible length", n, centre)
    return None


def to_ss_list(actor, cap, log):
    """lobby -> Mission Room -> Special -> SS Training. False if it stalls."""
    tp.game_scroll(cap, 0.0)
    time.sleep(0.4)
    if not tp.click_when(actor, cap, tp._tpl("mission_room_entry"),
                         "enter Mission Room"):
        log.info("could not find the Mission Room entrance")
        return False
    if not tp.click_when(actor, cap, tp._tpl("special_tab"), "Special tab"):
        log.info("could not find the Special tab")
        return False
    # SS Training has no template of its own yet; it is one row below TP
    # Training, whose position the bot already hits reliably.
    actor.click_pixel(*SS_ROW_XY, why="SS Training")
    time.sleep(3.0)
    if not K.find_rows(cap.frame(gray=False))[1] and \
            not tp.find_mission_rows(cap.frame(gray=False)):
        log.info("the SS list did not open")
        return False
    return True


def open_puzzle(actor, cap, log, tries=6):
    """Get from a freshly started mission to the puzzle panel.

    A mission opens on a cutscene and then a RULES PANEL - the game explaining
    its own feedback mapping - which has to be closed before the puzzle is
    reachable. `close_popup_x` matches it at 0.936.
    """
    for _ in range(tries):
        f = cap.frame(gray=False)
        if K.find_rows(f)[0] is not None:
            return True
        if tp.advance_cutscenes(actor, cap, log, limit=2):
            continue
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        for name in ("close_popup_x", "close_popup_x_menu", "close_promo_x"):
            t = tp._tpl(name, 0.80)
            if t is None:
                continue
            m, c = find(g, t)
            if m.found:
                actor.click_pixel(*m.center,
                                  why=f"close the SS rules panel ({name} {c:.3f})")
                time.sleep(2.2)
                break
        else:
            time.sleep(1.5)
    return K.find_rows(cap.frame(gray=False))[0] is not None


def play(cap, actor, log, max_stages=10, seed_history=None):
    """Play every stage of an open SS rune mission.

    Returns (stages_cleared, outcome, history) where outcome is one of
    "cleared", "failed", "stalled" or "lost", and `history` is what the current
    stage had answered when it stopped.

    **THE HISTORY IS CARRIED, because rows are the scarce resource.** A stage
    allows ten guesses and running out fails the whole MISSION, not just the
    stage. Without carrying it, every restart after an unreadable digit was
    harvested replayed the same openers into fresh rows - and at length five
    the solver needs about six of the ten, so two wasted restarts lose the
    mission. `seed_history` resumes a stage already part-answered.
    """
    cleared = 0
    hist = list(seed_history or [])
    for _ in range(max_stages):
        f = cap.frame(gray=False)

        # A DIALOG DECIDES WHETHER THERE IS A NEXT STAGE. Check it before the
        # puzzle: a solved stage leaves the panel closed and the dialog up, and
        # "the panel is gone" alone cannot tell a clear from a failed mission.
        d = stage_dialog(f)
        if d:
            kind, xy = d
            actor.click_pixel(*xy, why=f"{kind} -> OK")
            time.sleep(3.0)
            if kind == "fail":
                log.info("SS: the mission FAILED after %d stage(s) cleared",
                         cleared)
                return cleared, "failed", hist
            cleared += 1
            hist = []            # a new stage starts with a fresh scroll
            log.info("SS: stage %d cleared", cleared)
            continue

        if K.find_rows(f)[0] is None:
            log.info("SS: no puzzle and no dialog on screen - stopping rather "
                     "than clicking blind")
            return cleared, "lost", hist

        n = stage_length(f, log)
        if n is None:
            return cleared, "stalled", hist

        def _keep(h):
            hist[:] = h

        secret, used = K.solve_live(cap, actor, log, length=n,
                                    history=hist, on_history=_keep)
        if secret is None:
            log.info("SS: stage not solved (%s guess(es)) - stopping with %d "
                     "answer(s) kept for a resume", used, len(hist))
            # Give the dialog a moment; a failed stage raises one too.
            time.sleep(2.5)
            d = stage_dialog(cap.frame(gray=False))
            if d and d[0] == "fail":
                actor.click_pixel(*d[1], why="fail -> OK")
                return cleared, "failed", hist
            return cleared, "stalled", hist
        hist = []
    return cleared, "cleared", hist
