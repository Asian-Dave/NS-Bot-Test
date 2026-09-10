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

import balance  # noqa: E402
import kekkai_play as K  # noqa: E402
import lights  # noqa: E402
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
DIALOG_MIN_AREA = 15000
DIALOG_ASPECT = (1.5, 5.0)                 # a wide button, not a disc
# AND IT MUST BE SOLID. The village has a red sign in this very band -
# measured area 7631, 337x121, aspect 2.79 - which passed the old
# area>=2500 gate and made `stage_dialog` report a MISSION FAIL on the
# LOBBY. Both drivers check the dialog before anything else, so that would
# have clicked the village and abandoned a mission that was still running.
#
# Bbox alone cannot separate them (337x121 against the real 352x108). FILL
# can, and by a wide margin, because an OK button is a filled rounded rect
# while village art is outlines and lettering:
#
#     Stage Clear  31542 / (351*108) = 0.83
#     Mission Fail 29891 / (352*108) = 0.79
#     village sign  7631 / (337*121) = 0.19
#
# Same solid-not-outline test that `find_confirm_point` needed for the same
# reason - see the note there about a dark RING scoring as a disc.
DIALOG_MIN_FILL = 0.55
# AND IT IS ONE FIXED-SIZE ASSET. Fill alone still let two screens through,
# and one of them is the dangerous one:
#
#     character select  323x156  aspect 2.07  fill 0.65   <- Delete is here
#     TP mission list   445x105  aspect 4.24  fill 0.88
#     Stage Clear       351x108  aspect 3.25  fill 0.83
#     Mission Fail      352x108  aspect 3.26  fill 0.79
#
# A blind click on character select is the one this project has a hard rule
# about - `Delete` sits beside `Play`, which is why Play is only ever clicked
# BY TEMPLATE. A red blob passing as an OK button there is exactly the offset
# click that rule forbids, so the gate is closed on measurement rather than on
# aspect, which cannot separate 2.07/3.25/4.24 without being fitted to them.
#
# Width and height are independent and both discriminate: the viewport is
# pinned, so the game draws this button at one size, the way the command discs
# are one size.
DIALOG_W = (300, 400)                      # measured 351, 352
DIALOG_H = (85, 130)                       # measured 108, 108

# --- THE HINTS PANEL, AND ITS BUTTON IS DRAWN BELOW THE VIEWPORT ---------
#
# Two SS families - Lights Out and Balance Control - open on a HINTS panel
# that illustrates their rules, and it has **no X**. Measured on all five
# saved frames: `close_popup_x` 0.608/0.665, `close_popup_x_menu` 0.620/0.610,
# `close_promo_x` 0.466/0.454, `mission_start` 0.598/0.502 - nothing fires, so
# there is nothing for `open_puzzle`'s X sweep to press and the mission was
# abandoned as unrecognised. The only exit is a wide GREEN BUTTON at the
# bottom of the panel.
#
# **And that button does not fit on screen.** The game is 839 CSS px tall in a
# 720 px viewport, so its bottom 238 captured px are hidden, and the button
# lands exactly there: measured tops y=1404 and y=1410 against a frame that
# ends at 1440, leaving a 30..36 px sliver. Hence two things:
#
#   * the band starts at 1350 and the blob must be that low - the same colour
#     range also catches the puzzle's own green artwork at y=1300 (884x43 and
#     440x101), and a width window alone would let the 440 through;
#   * the click aims near the blob's TOP, not its centre, because the centre of
#     a clipped button is off screen. A Flash button's hit area is the whole
#     button, so the sliver is as good as the middle.
#
# The width is the positive signal: both panels draw it at 412 px, and nothing
# else in the reference set (21 TP, 26 mission, lobby, panels, battle frames)
# produces a wide green blob this low.
HINTS_BAND = (1000, 1350, 2600, 1440)
HINTS_GREEN = ((35, 90, 90), (85, 255, 255))
HINTS_W = (360, 480)
HINTS_MIN_AREA = 4000
HINTS_CLICK_INSET = 22


def hints_button(frame):
    """(x, y) to press on an SS hints panel, or None. See the note above."""
    x0, y0, x1, y1 = HINTS_BAND
    h, w = frame.shape[:2]
    x0, x1 = max(0, min(x0, w)), max(0, min(x1, w))
    y0, y1 = max(0, min(y0, h)), max(0, min(y1, h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, np.array(HINTS_GREEN[0], np.uint8),
                    np.array(HINTS_GREEN[1], np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, _, st, ce = cv2.connectedComponentsWithStats(m)
    best = None
    for i in range(1, n):
        a = st[i, cv2.CC_STAT_AREA]
        bw = st[i, cv2.CC_STAT_WIDTH]
        bh = st[i, cv2.CC_STAT_HEIGHT]
        if a < HINTS_MIN_AREA or bh == 0:
            continue
        if not (HINTS_W[0] <= bw <= HINTS_W[1]):
            continue
        if bw / bh < 2.5:
            continue
        if best is None or a > best[0]:
            top = int(st[i, cv2.CC_STAT_TOP]) + y0
            best = (a, int(ce[i][0]) + x0,
                    top + min(int(bh) // 2, HINTS_CLICK_INSET))
    return (int(best[1]), int(best[2])) if best else None


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
        if a < DIALOG_MIN_AREA or bh == 0 or bw == 0:
            continue
        if not (DIALOG_ASPECT[0] <= bw / bh <= DIALOG_ASPECT[1]):
            continue
        if not (DIALOG_W[0] <= bw <= DIALOG_W[1]):
            continue
        if not (DIALOG_H[0] <= bh <= DIALOG_H[1]):
            continue
        if a / float(bw * bh) < DIALOG_MIN_FILL:
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
    f = cap.frame(gray=False)
    if tp.find_mission_rows(f):
        return True
    # AN EMPTY LIST IS NOT A NAVIGATION FAILURE, and calling it one is the
    # negative-definition trap this project records five times over: "no rows
    # matched" is not evidence that the panel failed to open.
    #
    # **COMPLETED SS MISSIONS DROP OUT OF THE DAY'S LIST**, unlike TP's, which
    # stay listed and go grey. Measured across one afternoon: five rows over
    # two pages, then two rows on one page, then a panel reading `1 / 0` with
    # no rows at all. So an exhausted SS day looks exactly like a list that
    # never opened, and `to_ss_list` reported "the SS list did not open" about
    # a perfectly healthy panel - which would have `run_all` blame navigation
    # for having nothing left to do.
    #
    # The list UI carries its own anchors, all three at 1.000 on the empty
    # panel against 0.18..0.35 for anything row-shaped, so the panel's presence
    # is a positive reading rather than an inference.
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    for name in ("list_back_arrow", "page_next", "page_prev"):
        t = tp._tpl(name)
        if t is not None and find(g, t)[0].found:
            log.info("the SS list is open and EMPTY - today's SS missions are "
                     "all done (%s 1.000)", name)
            return True
    log.info("the SS list did not open")
    return False


def open_puzzle(actor, cap, log, tries=6):
    """Get from a freshly started mission to the puzzle panel.

    A mission opens on a cutscene and then a panel of RULES - the game
    explaining its own feedback mapping - which has to be dismissed before the
    puzzle is reachable. There are two shapes of it, and only one has an X:

        rune (Sage Power Seal)      an X, `close_popup_x` at 0.936
        Lights Out / Balance        a green button, and NO X at all

    The X sweep is tried first only because it is the cheaper test; the two are
    measured not to overlap (see `hints_button`), so the order cannot matter.

    Returns True only when the RUNE scroll is up. Everything else is left for
    `identify` to name off the screen - dismissing the panel is this function's
    whole contribution to the families it cannot play.
    """
    for _ in range(tries):
        f = cap.frame(gray=False)
        if K.find_rows(f)[0] is not None:
            return True
        if tp.advance_cutscenes(actor, cap, log, limit=2):
            continue
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        closed = False
        for name in ("close_popup_x", "close_popup_x_menu", "close_promo_x"):
            t = tp._tpl(name, 0.80)
            if t is None:
                continue
            m, c = find(g, t)
            if m.found:
                actor.click_pixel(*m.center,
                                  why=f"close the SS rules panel ({name} {c:.3f})")
                time.sleep(2.2)
                closed = True
                break
        if closed:
            continue
        hb = hints_button(f)
        if hb:
            actor.click_pixel(*hb, why="the SS hints panel's green button")
            time.sleep(2.2)
            continue
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
    cleared, blank = 0, 0
    hist = list(seed_history or [])
    for _ in range(max_stages * (BLANK_TOLERANCE + 4)):
        if cleared >= max_stages:
            break
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
            blank = 0
            hist = []            # a new stage starts with a fresh scroll
            log.info("SS: stage %d cleared", cleared)
            continue

        if K.find_rows(f)[0] is None:
            # THE SAME ENDING THE PUZZLE DRIVERS NEEDED, and the rune family
            # was left without it. Measured live: guess 6 solved the stage
            # ("panel closed after guess 6 -> SOLVED"), `Mission Success!` was
            # on screen three seconds later, and this branch fired in between
            # and reported "lost" - so `run_all` logged "mission did not
            # complete; it stays in the list and will not be retried this
            # pass" about a mission it had just WON.
            if mission_over(f):
                log.info("SS: the mission is over - Mission Success is up")
                return cleared, "cleared", hist
            blank += 1
            if blank <= BLANK_TOLERANCE:
                time.sleep(BLANK_POLL)
                continue
            log.info("SS: no puzzle and no dialog for %d frame(s) - stopping "
                     "rather than clicking blind", blank)
            return cleared, "lost", hist
        blank = 0

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


def identify(frame, tpls=None):
    """What an opened SS mission is: "rune", "balance", "combat" or None.

    BY LOOKING, NOT BY NAME. `Sage Power Seal` is the rune Mastermind and
    `Twins Unicorn` is a straight fight against two Lv 80 enemies - both sit in
    the same list, and the title only hints. This project already made the
    name-matching mistake for TP and records the rule: the pixels win.
    """
    if K.find_rows(frame)[0] is not None:
        return "rune"
    if balance.board_present(frame) is not None:
        return "balance"
    if lights.locate(frame) is not None:
        return "lights"
    if tpls is not None:
        import farm as farm_mod
        if farm_mod.in_mission(frame, tpls):
            return "combat"
    return None


def mission_over(frame):
    """True when the `Mission Success!` panel is up.

    A ONE-STAGE mission ends straight on the reward panel with no stage dialog
    at all - `Sage Sealed Boxes` cleared in five presses and went directly
    there - and the panel is not an OK button, so `stage_dialog` cannot see it.
    Without this the drivers wait out their blank tolerance and report "lost"
    about a mission that has just been WON.
    """
    t = tp._tpl("mission_success")
    if t is None:
        return False
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return bool(find(g, t)[0].found)


# A STAGE ENDING IS NOT INSTANT. Both puzzle families clear their board and
# then take a moment to deal the next stage or raise the dialog, so a driver
# that judged the very next frame reported "no board and no dialog" on a stage
# it had just won - measured on Lights Out, which went dark in five presses and
# was then abandoned one frame later. Blank frames are tolerated for a bounded
# stretch, and only a sustained blank is a genuine loss.
BLANK_TOLERANCE = 8        # consecutive frames with neither board nor dialog
BLANK_POLL = 1.0


def play_balance(cap, actor, log, max_stages=12):
    """Play every stage of an open Balance Control mission. (cleared, outcome).

    Same shape as `play` for the rune puzzle, and for the same reason: the
    dialog is checked BEFORE the board, because a finished stage leaves no
    board on screen and "the board is gone" alone cannot tell a clear from a
    timed-out mission.

    **EVERY STAGE IS ON A CLOCK** (169 s observed), so nothing here waits on
    anything it can compute - see the note at the top of `balance.py`, which
    exists because the first live look cost the mission by measuring offline
    and coming back to click.
    """
    cleared, blank = 0, 0
    for _ in range(max_stages * (BLANK_TOLERANCE + 4)):
        f = cap.frame(gray=False)
        if cleared >= max_stages:
            break
        d = stage_dialog(f)
        if d:
            kind, xy = d
            actor.click_pixel(*xy, why=f"{kind} -> OK")
            time.sleep(3.0)
            if kind == "fail":
                # SAVE THE DIALOG. The first stage-2 failure had every answer
                # arithmetically correct and was followed by `Mission
                # Success!`, so what this red button actually says is not yet
                # established - and a dialog is gone by the time an operator
                # looks.
                try:
                    d2 = os.path.join(ROOT, "ref/auto/ss")
                    os.makedirs(d2, exist_ok=True)
                    n = len([x for x in os.listdir(d2)
                             if x.startswith("balance_fail")])
                    if n < 4:
                        pth = os.path.join(
                            d2, f"balance_fail_{int(time.time())}.png")
                        cv2.imwrite(pth, f)
                        log.info("SS balance: saved the red dialog to %s",
                                 os.path.relpath(pth, ROOT))
                except Exception as e:
                    log.warning("SS balance: could not save the dialog: %s", e)
                log.info("SS balance: the mission FAILED after %d stage(s)",
                         cleared)
                return cleared, "failed"
            cleared += 1
            blank = 0
            log.info("SS balance: stage %d cleared", cleared)
            continue
        if balance.board_present(f) is None:
            if mission_over(f):
                log.info("SS balance: the mission is over - Mission Success is up")
                return cleared, "cleared"
            blank += 1
            if blank <= BLANK_TOLERANCE:
                time.sleep(BLANK_POLL)
                continue
            log.info("SS balance: no board and no dialog for %d frame(s) - "
                     "stopping rather than clicking blind", blank)
            return cleared, "lost"
        blank = 0
        won, verdict = balance.play(cap, actor, log,
                                    over=lambda fr: stage_dialog(fr) is not None)
        log.info("SS balance: %d board(s) balanced this stage (%s)",
                 won, verdict)
        if verdict not in ("gone", "capped"):
            time.sleep(2.5)
            if stage_dialog(cap.frame(gray=False)) is None:
                return cleared, verdict
    return cleared, "cleared"


def play_lights(cap, actor, log, max_stages=12):
    """Play every stage of an open Lights Out mission. (cleared, outcome).

    Same shape as `play_balance`, and the dialog is checked BEFORE the board
    for the same reason: a finished stage leaves no board on screen, and "the
    board is gone" alone cannot tell a clear from a timed-out mission.
    """
    cleared, blank = 0, 0
    for _ in range(max_stages * (BLANK_TOLERANCE + 4)):
        f = cap.frame(gray=False)
        if cleared >= max_stages:
            break
        d = stage_dialog(f)
        if d:
            kind, xy = d
            actor.click_pixel(*xy, why=f"{kind} -> OK")
            time.sleep(3.0)
            if kind == "fail":
                log.info("SS lights: the mission FAILED after %d stage(s)",
                         cleared)
                return cleared, "failed"
            cleared += 1
            blank = 0
            log.info("SS lights: stage %d cleared", cleared)
            continue
        if lights.locate(f) is None:
            if mission_over(f):
                log.info("SS lights: the mission is over - Mission Success is up")
                return cleared, "cleared"
            blank += 1
            if blank <= BLANK_TOLERANCE:
                time.sleep(BLANK_POLL)
                continue
            log.info("SS lights: no board and no dialog for %d frame(s) - "
                     "stopping rather than clicking blind", blank)
            return cleared, "lost"
        blank = 0
        presses, verdict = lights.play(
            cap, actor, log, over=lambda fr: stage_dialog(fr) is not None)
        log.info("SS lights: %d press(es), %s", presses, verdict)
        if verdict not in ("cleared", "gone"):
            time.sleep(2.5)
            if stage_dialog(cap.frame(gray=False)) is None:
                return cleared, verdict
    return cleared, "cleared"


def run_one(cap, actor, log, tpls=None, play_combat=None, relog=None):
    """Play ONE already-started SS mission through to its reward. True if banked.

    Dispatches on what is on screen. Combat is handed to `play_combat` - the
    supervisor's own mission runner - so SS fights inherit the entire battle
    stack rather than a second copy of it: the panel's skill rotation, the
    greyed-slot skip, cooldown learning, the stun fallback to Dodge and
    DamageWatchdog. Verified live on `Twins Unicorn`, which the shared runner
    cleared using the rotation set for farming.
    """
    if not open_puzzle(actor, cap, log, tries=5):
        pass                      # not a rune puzzle; identify below decides
    f = cap.frame(gray=False)
    kind = identify(f, tpls)
    log.info("SS: this mission is %s", kind or "not recognised")
    if kind == "rune":
        cleared, outcome, _hist = play(cap, actor, log)
        log.info("SS: %d stage(s) cleared, %s", cleared, outcome)
    elif kind in ("balance", "lights"):
        # THE DRIVER'S VERDICT DOES NOT DECIDE WHETHER ANYTHING BANKED, and
        # letting it do so lost a completed mission: Lights Out cleared in five
        # presses, the driver said "lost" because no board and no stage dialog
        # followed, and `Mission Success! 10,000 gold` was on screen at the
        # time. `close_out` is the measurement that establishes a banked
        # mission, so it is asked either way and its answer is the answer.
        # NOT `play = ...`: this function also calls the module-level `play`
        # for the rune family, and assigning that name ANYWHERE in a function
        # makes it local for the WHOLE function - so the rune branch raised
        # UnboundLocalError before it could run. Same shape as the `arrow` bug
        # in `_traverse`, and caught the same way: by executing it, not by
        # reading it.
        driver = play_balance if kind == "balance" else play_lights
        cleared, outcome = driver(cap, actor, log)
        log.info("SS: %d %s stage(s) cleared, %s", cleared, kind, outcome)
    elif kind == "combat":
        if play_combat is None:
            log.info("SS: this is a battle and no battle runner was supplied")
            return False
        play_combat()
    else:
        # SAVE THE SCREEN. Three of the five SS missions have never been
        # opened, and every unrecognised screen in this project turned out to
        # be one anchor from handled - the hard part was always catching it.
        try:
            d = os.path.join(ROOT, "ref/auto/ss")
            os.makedirs(d, exist_ok=True)
            n = len([x for x in os.listdir(d) if x.startswith("unknown")])
            if n < 6:
                p = os.path.join(d, f"unknown_{int(time.time())}.png")
                cv2.imwrite(p, f)
                log.info("SS: saved this screen to %s so it can be taught",
                         os.path.relpath(p, ROOT))
        except Exception as e:
            log.warning("SS: could not save the screen: %s", e)
        return False
    return tp.close_out(actor, cap, log)


def run_all(cap, actor, log, tpls=None, play_combat=None, relog=None,
            max_missions=None):
    """Play every startable SS mission on the day's list. (played, banked).

    The sweep itself is `tp.run_all` - shared deliberately, because that loop
    carries fixes paid for in lost missions (reflow-safe fingerprints,
    failure-only memory, a tripwire on a broken termination check) and a second
    copy would be a second place to get them wrong.
    """
    return tp.run_all(
        cap, actor, log, relog=relog, max_missions=max_missions,
        to_list=to_ss_list,
        run_one_fn=lambda: run_one(cap, actor, log, tpls=tpls,
                                   play_combat=play_combat, relog=relog),
        label="SS")
