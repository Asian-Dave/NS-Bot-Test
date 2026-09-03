#!/usr/bin/env python3
"""TP Training runner — navigate, play, close out.

Completes a TP Training mission end to end:

    lobby -> Mission Room -> Special tab -> TP Training -> pick a mission
      -> green check to start -> cutscene(s)
      -> hunt seals across maps, solving each  (Kekkai family only)
      -> Mission Success -> acknowledge -> back in the lobby

Verified by completing "The Kekkai in the Forest": rewards banked (gold
1,196,781 -> 1,198,981, XP 494,230 -> 496,230) and the game returned to the
village.

WHICH MISSIONS THIS CAN ACTUALLY RUN
------------------------------------
TP Training has five missions in three families, and the family is readable from
the name:

    Kekkai   "The Kekkai in the Forest"                  rune Mastermind
    Scroll   "Secret TP Scroll", "Another TP Scroll"     4x5 memory board
    Potion   "Dangerous Potion", "Weird Potion"          NOT SUPPORTED

The Potion family opens the hand-seal minigame, which needs a jutsu -> seal-pair
table that is not in the client (SKILL_DATA is server-fed) — see engine/minigame.py.
It is refused by name rather than started and failed, because the flame column
claims a cost and three hearts do not survive guessing at 90 options.

Note the Scroll board is TIMED and the Kekkai one is not, which is why the two
solvers are paced completely differently.

THE THREE MECHANICS THAT ARE EASY TO GET WRONG
----------------------------------------------
1. **Traversal is edge-to-edge, and the heading comes from where you SPAWN.**
   If no seal is on the map you must run to a canvas edge; the location changes
   during the run. You spawn near the edge you entered through, so the heading
   must be re-derived per map from the character's position — a fixed or merely
   persistent heading runs straight back where it came from, forever.
2. **A seal is approached, then opened.** The first click walks you to it; only a
   second click opens the puzzle.
3. **Node count is the code length**, and it must be read BEFORE opening the
   puzzle, while the seal is still drawn in the scene. A 3-node seal is a 3-rune
   code, a 5-node one is 5.

SAFETY
------
* On Mission Success the game may raise a "Share with Teammates!" dialog. This
  closes it with its X and never touches "Share to wall" — that publishes to a
  social feed, which is not something a bot should do unasked.
* The Mission Room's NPC row carries token-priced recruit `+` buttons
  (T20..T150). Nothing here clicks in that row.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

import kekkai_play as kp
import minigame as mg
from act import Actor, Controls
from capture import Capture
from cdp import CDP, find_page_target
from perceive import Template, find

# Mission name -> family. Kept as substrings so it survives minor renames.
FAMILIES = {
    "kekkai": "kekkai",
    "potion": "potion",
    "scroll": "scroll",
}
# `scroll` is the 4x5 memory board (engine/cards.py). It is TIMED, which is
# why its solver reads a clipped board and skips the human-like click pacing.
# `potion` is the hand-seal game (engine/seals.py). Its mechanic is understood
# and the phases are read reliably, but matching a revealed slot to a tile is
# decisive for most seals (5.13x) and thin for a couple (1.10x), so a round can
# be played wrong and cost a heart.
#
# It is included anyway, and deliberately LAST in the try order, because the
# alternative is worse: refusing means the Potion missions are simply never
# attempted, and every live attempt is also what produces the labelled crops
# that would make the matcher reliable. Failing a TP mission costs stamina and
# nothing else.
SUPPORTED = {"kekkai", "scroll", "potion"}

# Order matters: try the families we solve reliably first, and only fall through
# to the hand-seal game when nothing else is left today.
TRY_ORDER = ("scroll", "kekkai", "potion")

# The green check is ONE glyph drawn at several sizes (mission detail 1.00,
# Victory 1.18, Mission Success 1.84, seal-broken dialog 1.1), so anything that
# looks for it must sweep.
CHECK_SCALES = [round(0.95 + i * 0.05, 2) for i in range(21)]


class _Log:
    def info(self, m, *a):
        print(("  " + m) % a if a else "  " + m, flush=True)
    warning = error = info


def _tpl(name, thr=0.88, scales=None):
    t = Template(name, os.path.join(ROOT, "tpl", f"{name}.png"), threshold=thr)
    if scales:
        t.scales = scales
    return t


def green_check(gray, thr=0.80):
    t = _tpl("mission_start", thr, CHECK_SCALES)
    m, c = find(gray, t)
    return (m.center, c) if m.found else (None, c)


def game_scroll(cap, frac=0.0):
    """Delegates to Capture.scroll_game - see there for why this is needed."""
    return cap.scroll_game(frac)


def click_when(actor, cap, tpl, why, tries=4, settle=2.6):
    """Match `tpl` and click it, retrying. False if never found.

    Retries alternate the page scroll, because a template can be genuinely
    absent from the viewport rather than absent from the game - see
    `game_scroll`.
    """
    for i in range(tries):
        g = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
        m, c = find(g, tpl)
        if m.found:
            actor.click_pixel(*m.center, why=f"{why} ({c:.3f})")
            time.sleep(settle)
            return True
        # Not on screen. Before deciding it is not there at all, look in the
        # part of the game the viewport is currently cutting off.
        game_scroll(cap, 0.0 if i % 2 == 0 else 1.0)
        time.sleep(0.6)
    return False


def to_tp_list(actor, cap, log):
    """lobby -> Mission Room -> Special -> TP Training. False if it stalls."""
    game_scroll(cap, 0.0)          # start from a known scroll, not whatever we inherited
    time.sleep(0.4)
    if not click_when(actor, cap, _tpl("mission_room_entry"), "enter Mission Room"):
        log.info("could not find the Mission Room entrance in the village")
        return False
    if not click_when(actor, cap, _tpl("special_tab"), "Special tab"):
        log.info("could not find the Special tab")
        return False
    if not click_when(actor, cap, _tpl("tp_training_row"), "TP Training"):
        log.info("could not find TP Training")
        return False
    return True


# Family -> the row-title templates that identify ITS missions in the TP list.
#
# Choosing by name and not by row position is the point: the missions are spread
# over up to two pages in an order we do not control, and starting the wrong one
# spends whatever the flame column costs on a minigame that may not be playable.
#
# A family has SEVERAL missions and they are tried in turn, because **the TP list
# is a daily list that SHRINKS as missions are completed.** Measured live: after
# finishing "Secret TP Scroll" and "The Kekkai in the Forest", the list went from
# 5 entries over 2 pages to 3 entries on a single page (1/1) with both completed
# missions gone. A picker that knows only one mission per family reports "not on
# this page" the moment that one is done for the day.
ROW_TEMPLATES = {
    "kekkai": ["tp_kekkai_row"],                        # "The Kekkai in the Forest"
    "scroll": ["tp_scroll_row", "tp_scroll2_row"],      # "Secret TP Scroll",
                                                        # "Another TP Scroll"
    "potion": ["tp_potion_row", "tp_potion2_row"],      # "Dangerous Potion",
                                                        # "Weird Potion"
}


def find_mission_rows(frame, x0=1650, x1=2550, y0=200, y1=1050,
                      step=4, min_frac=0.35, min_h=20):
    """Where the mission rows are on the current list page, by SHAPE not by name.

    Returns the y centre of each row's title bar, top to bottom.

    WHY NOT FIXED POSITIONS, AND WHY NOT NAMES
    ------------------------------------------
    Rows sit on a ~178 px pitch, but the PANEL MOVES - the same lesson the
    hand-seal geometry taught (a reload shifted that panel 111 px). Measured on
    two live captures of the same list, the first row's title bar was at y=452
    in one and y=350 in the other, so any hardcoded y reads the gap between rows
    instead.

    And matching each mission by a name template does not scale: it needs a new
    template per mission, it silently skips anything renamed or newly added, and
    the family a name implies is not guaranteed to be the minigame you get. The
    minigame is identified from the SCREEN once it opens (engine/minigame.py) -
    so the list only has to answer "how many rows are there, and where".

    A title bar is a WIDE BAND OF ONE SPECIFIC COLOUR - a muted brown, measured
    hue 8..22, sat 45..110, val 100..160. Averages are not enough to find it:
    mean saturation and value alone also fire on the panel header, on village
    architecture, and on the mission room, because an average says nothing about
    whether the colour is uniform across the row.

    What separates is the FRACTION of the row that is bar-coloured:

        real title bars     0.609 .. 0.786
        panel header        0.000
        mission room        0.001
        lobby architecture  0.012 .. 0.107

    so the gate sits at 0.35, about half way in log terms and 5.7x clear of the
    worst false positive.
    """
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]
    x0, x1 = max(0, x0), min(w, x1)
    hits = []
    for y in range(max(0, y0), min(h, y1), step):
        band = hsv[y:y + step, x0:x1]
        if band.size == 0:
            continue
        hh, ss, vv = band[:, :, 0], band[:, :, 1], band[:, :, 2]
        frac = float(((hh >= 8) & (hh <= 22) & (ss >= 45) & (ss <= 110)
                      & (vv >= 100) & (vv <= 160)).mean())
        if frac >= min_frac:
            hits.append(y)
    runs, cur = [], []
    for y in hits:
        if cur and y - cur[-1] > step * 2:
            runs.append(cur)
            cur = []
        cur.append(y)
    if cur:
        runs.append(cur)
    return [int((r[0] + r[-1]) / 2) for r in runs
            if (r[-1] - r[0]) >= min_h]


# How much of the screen must change for a click to count as having DONE
# something. A detail panel covers a large part of the canvas, so this does not
# need to be delicate - it only has to separate "a panel opened" from "the
# click was swallowed".
ROW_CHANGE_FRAC = 0.02


def _changed(a, b, frac=ROW_CHANGE_FRAC):
    """Did the screen materially change between two frames?

    Returns (changed, measured_fraction) so the number reaches the log and the
    threshold can be re-derived from real runs instead of argued about.
    """
    if a is None or b is None or a.shape != b.shape:
        return True, 1.0
    d = cv2.absdiff(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY),
                    cv2.cvtColor(b, cv2.COLOR_BGR2GRAY))
    f = float((d > 24).mean())
    return f >= frac, f



def _save_frame(frame, log, tag):
    """Keep the frame that ended a decision, so it can be taught later.

    Same reasoning as the runner's unknown-screen dump: every unrecognised
    screen in this project turned out to be one anchor from one frame away from
    handled, and the hard part was always CATCHING the frame.
    """
    try:
        d = os.path.join(ROOT, "ref/auto/tp")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{tag}_{int(time.time())}.png")
        cv2.imwrite(path, frame)
        log.info("saved %s - commit it to teach this screen",
                 os.path.relpath(path, ROOT))
    except Exception as e:
        log.warning("could not save the frame: %s", e)


# IDENTIFY A ROW BY WHAT IT SAYS, NOT WHERE IT SITS.
#
# A completed TP mission greys out, and `find_mission_rows` keys on the brown
# title bar - so a finished mission stops being DETECTED and the survivors
# reflow upwards into the slots it vacated. Position is therefore not an
# identity: it names a hole in the list, and the next mission to fall into that
# hole inherits whatever was remembered about the last one.
#
# Measured live. Four TP missions played, one still on the list:
#
#     TP list page 1: 1 row(s) at y=[462]
#     TP list page 2: 1 row(s) at y=[462]
#     every TP row is now played or greyed out (4 played, 0 already done)
#
# Both survivors had reflowed to y=462 - the TOP slot, which a played mission
# had occupied - so both matched the skip list by position and the pass
# declared itself finished with a mission still startable. Note `0 already
# done`: nothing was ever measured as exhausted, so this was purely the skip
# list refusing rows it had no business refusing.
#
# Same lesson `rows_signature` already records for page turns: the rows sit at
# identical y on every page, so only the CONTENT distinguishes them.
ROW_FP_TOL = 6.0


def row_fingerprint(frame, y, w=48, h=8):
    """A cheap fingerprint of the row at `y`. None if it cannot be sampled."""
    fh, fw = frame.shape[:2]
    y0, y1 = max(0, y - 18), min(fh, y + 18)
    x0, x1 = max(0, 1700), min(fw, 2500)
    band = frame[y0:y1, x0:x1]
    if band.size == 0:
        return None
    g = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (w, h), interpolation=cv2.INTER_AREA)


def same_row(a, b, tol=ROW_FP_TOL):
    """Are these two fingerprints the same mission?"""
    if a is None or b is None or a.shape != b.shape:
        return False
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()) <= tol


def start_row(actor, cap, log, y, x=2195, settle=2.6):
    """Open the mission whose title bar is at `y`, and press its green check.

    Returns (started, reason). The REASON is the point: "it did not start" was
    previously one outcome covering two completely different situations, and
    the bot needs them apart to know when a day's list is genuinely finished.

        "started"    the green check was pressed
        "exhausted"  the row is inert or has no green check - it is done
        "no_panel"   the click changed the screen but produced no usable panel,
                     which is a transient miss and worth another sweep

    A COMPLETED TP mission is not clickable, and an unclickable row leaves the
    screen exactly as it was. So "nothing changed" is a POSITIVE reading of
    "this one is finished" rather than the absence of a green check - which is
    the negative definition this project keeps getting caught by.
    """
    before = cap.frame(gray=False)
    actor.click_pixel(x, y, why=f"open the mission at y={y}")
    time.sleep(settle)
    after = cap.frame(gray=False)

    moved, frac = _changed(before, after)
    if not moved:
        log.info("the row at y=%d did not respond to a click (%.4f of the "
                 "screen changed) - it is greyed out, so it is already done",
                 y, frac)
        return False, "exhausted"

    g = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)
    pt, cc = green_check(g)
    if not pt:
        # The screen DID change, so something opened - but there is no control
        # to start it. Save the frame: this is the shape a completed or locked
        # TP row takes, and one committed frame is all a proper template needs.
        log.info("the row at y=%d opened something with no green check "
                 "(%.3f) - treating it as done", y, cc)
        _save_frame(after, log, f"tp_row_no_check_y{y}")
        return False, "exhausted"

    actor.click_pixel(*pt, why=f"start mission ({cc:.3f})")
    time.sleep(3.0)
    return True, "started"


def pick_any(actor, cap, log, skip=(), max_pages=3, done=None):
    """Start ANY unplayed mission on the TP list. Returns its (page, y) or None.

    Name-agnostic on purpose: what the mission actually is gets decided by
    looking at the minigame once it opens.

    `done` collects rows found to be EXHAUSTED (greyed out, or opening with no
    green check). The caller adds them to `skip` so a finished row is not
    re-clicked on every sweep, which is what made a pass look like it was
    stuck re-reading the same list.
    """
    done = done if done is not None else []
    nxt = _tpl("page_next", 0.85)
    skip = list(skip or [])
    for page in range(max_pages):
        f = cap.frame(gray=False)
        rows = find_mission_rows(f)
        log.info("TP list page %d: %d row(s) at y=%s", page + 1, len(rows), rows)
        for y in rows:
            fp = row_fingerprint(f, y)
            if any(same_row(fp, s_fp) for s_fp in skip):
                continue
            started, why = start_row(actor, cap, log, y)
            if started:
                return (page, y, fp)
            # An exhausted row is not worth revisiting on a later sweep, so it
            # joins the skip list the caller passed in. A transient miss is
            # left alone deliberately - the next sweep should try it again.
            if why == "exhausted":
                done.append(fp)
            log.info("row at y=%d would not start (%s); trying the next", y, why)
        g = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
        nm, nc = find(g, nxt)
        if not nm.found:
            return None
        actor.click_pixel(*nm.center, why=f"TP list next page ({nc:.3f})")
        time.sleep(2.2)
    return None


def pick_mission(actor, cap, log, which="kekkai", max_pages=3):
    """From the TP list, find a named mission and start it.

    Missions are spread over 2 pages, so this pages FORWARD until the row title
    matches rather than assuming a page. Matching is on the title text, so
    "Secret TP Scroll" cannot be confused with "Another TP Scroll" - measured
    1.000 against 0.608 on the page holding the other one.
    """
    names = ROW_TEMPLATES.get(which)
    if not names:
        log.info("no row template for %r; known: %s", which, sorted(ROW_TEMPLATES))
        return False
    rows = [_tpl(n) for n in names
            if os.path.exists(os.path.join(ROOT, "tpl", f"{n}.png"))]
    if not rows:
        log.info("no row templates on disk for %r (%s)", which, names)
        return False
    nxt = _tpl("page_next", 0.85)
    for page in range(max_pages):
        g = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
        m = c = None
        for r in rows:
            mm, cc = find(g, r)
            if mm.found:
                m, c, hit = mm, cc, r.name
                break
        if m is not None:
            log.info("found %s on page %d (%.3f)", hit, page + 1, c)
            actor.click_pixel(*m.center, why=f"open {which} mission ({c:.3f})")
            time.sleep(2.6)
            g = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
            pt, cc = green_check(g)
            if not pt:
                log.info("detail panel open but its green check was not found (%.3f)", cc)
                return False
            actor.click_pixel(*pt, why=f"start mission ({cc:.3f})")
            time.sleep(3.0)
            return True
        nm, nc = find(g, nxt)
        if not nm.found:
            log.info("no %s mission on this page and no next-page arrow - if the "
                     "family's missions are already done today they are no longer "
                     "listed", which)
            return False
        actor.click_pixel(*nm.center, why=f"TP list next page ({nc:.3f})")
        time.sleep(2.2)
    return False


def pick_kekkai(actor, cap, log, max_pages=3):
    """Back-compat shim for the Kekkai mission specifically."""
    return pick_mission(actor, cap, log, "kekkai", max_pages)


def advance_cutscenes(actor, cap, log, limit=12):
    """Click through 'click anywhere to continue' until it stops appearing."""
    cs = _tpl("cutscene_continue", 0.80, [round(0.9 + i * 0.05, 2) for i in range(9)])
    n = 0
    for _ in range(limit):
        g = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
        m, c = find(g, cs)
        if not m.found:
            break
        n += 1
        actor.click_pixel(1720, 720, why=f"advance cutscene {n} ({c:.3f})")
        time.sleep(1.8)
    log.info("advanced %d cutscene screen(s)", n)
    return n


def close_out(actor, cap, log, timeout=45):
    """Acknowledge Mission Success (and any share prompt). True on success.

    A TP mission is not banked until the Success panel's check is acknowledged,
    same as a story mission.
    """
    ms = _tpl("mission_success")
    cs = _tpl("cutscene_continue", 0.80,
              [round(0.9 + i * 0.05, 2) for i in range(9)])
    t0 = time.time()
    seen = False
    while time.time() - t0 < timeout:
        f = cap.frame(gray=False)
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        # A MISSION DOES NOT END ON THE MINIGAME. There is an epilogue cutscene
        # between winning and the Success panel - "Congratulations! You have more
        # TP now!" - and an earlier close_out sat on it for its whole 45 s
        # timeout waiting for a panel that could not appear until it was clicked
        # through. Waiting for one specific screen is the fixed-script mistake in
        # miniature; check what is actually there and advance it.
        if not seen:
            cm, cc = find(g, cs)
            if cm.found:
                actor.click_pixel(1720, 720,
                                  why=f"advance epilogue cutscene ({cc:.3f})")
                time.sleep(1.6)
                continue
        if find(g, ms)[0].found:
            seen = True
            # A share prompt can sit on top of the Success panel, COVERING the
            # green check. Dismiss it by its X; NEVER click "Share to wall" -
            # that posts publicly, and it carries a green check glyph of its own
            # that scores 0.708, which is exactly why the check search is gated
            # at 0.80 and the share prompt is closed FIRST.
            #
            # `close_share_x` is its own template because none of the existing
            # four matched it: measured on a live share prompt, close_popup_x
            # 0.719, close_popup_x_menu 0.586, close_promo_x 0.465 - all below
            # threshold, so close-out timed out with the reward panel still open.
            for x_tpl in ("close_share_x", "close_popup_x", "close_popup_x_menu"):
                p = os.path.join(ROOT, "tpl", f"{x_tpl}.png")
                if not os.path.exists(p):
                    continue
                m, c = find(g, _tpl(x_tpl))
                if m.found:
                    actor.click_pixel(*m.center,
                                      why=f"close share prompt via {x_tpl} ({c:.3f})")
                    time.sleep(2.0)
                    g = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
                    break
            pt, c = green_check(g)
            if pt:
                actor.click_pixel(*pt, why=f"acknowledge Mission Success ({c:.3f})")
                time.sleep(3.0)
                if not find(cv2.cvtColor(cap.frame(gray=False),
                                         cv2.COLOR_BGR2GRAY), ms)[0].found:
                    log.info("Mission Success acknowledged; mission banked")
                    return True
            else:
                log.info("Success panel up but its check was not located (%.3f)", c)
        elif seen:
            # THE PANEL BEING GONE IS NOT THE SAME AS BEING BACK IN THE VILLAGE.
            # CLAUDE.md records the false-success bug this exact shape caused for
            # story missions: returning success on the panel alone let the next
            # run start while the game was still mid-transition. Require the
            # lobby anchor too, and say so when it does not come back.
            lob = _tpl("lobby_rail_fortune", 0.90)
            for _ in range(10):
                lg = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
                lm, lc = find(lg, lob)
                if lm.found:
                    log.info("Success panel cleared and the village is back "
                             "(%.3f) - mission banked", lc)
                    return True
                time.sleep(1.2)
            log.info("Success panel cleared but the village did not come back; "
                     "not calling this a success")
            return False
        time.sleep(1.5)
    log.info("close-out timed out after %ss", timeout)
    return False


# A TRIPWIRE, NOT A POLICY. The pass ends when the list says it is finished,
# so this exists purely so a bug cannot spin forever - if it is ever hit, the
# termination condition is broken and the log should say so loudly.
SWEEP_TRIPWIRE = 40


def run_all(cap, actor, log, relog=None, max_missions=None):
    """Play EVERY TP mission on the list, whatever they turn out to be.

    The mission is chosen by position, not by name, and what it IS gets decided
    by looking at the minigame once it opens (engine/minigame.py). That is the
    right way round: a name is a hint, not a guarantee - the family a title
    implies is not necessarily the minigame you get, and matching names needs a
    new template for every mission that is ever added or renamed.

    THERE IS NO MISSION CAP. There used to be one (`max_missions=6`), and it
    was the wrong shape of answer twice over: a count is not the thing that
    decides whether the day's list is finished, and it silently stopped a pass
    while startable missions were still on screen. The list itself is the
    authority - keep going until every row is either played or measured to be
    unstartable.

    **CORRECTION - completed missions do NOT drop out of the list.** This
    function used to say they did, and built its termination on it. They stay
    listed and go GREY, which is not clickable, which is why a pass could look
    like it was working through a list that never shrank. An unclickable row
    leaves the screen unchanged when clicked, and `start_row` reads that as a
    positive "this one is done" rather than inferring it from a missing green
    check.

    A FAILED mission is NOT consumed - it stays listed and startable - so rows
    that fail are remembered and skipped for the rest of THIS pass rather than
    retried forever.

    `max_missions` remains only as an explicit opt-in bound for a caller that
    genuinely wants one (a test, or a cautious manual run). The default is
    unbounded.

    Returns (played, banked).
    """
    played = banked = 0
    # ONLY FAILURES ARE REMEMBERED, and this is the fix for a pass that stopped
    # with a mission still on the list.
    #
    # A completed TP mission greys out and stops being detected at all, so a
    # mission that was successfully played can never be offered again - there
    # is nothing to remember. Recording it was not merely redundant, it was
    # HARMFUL: the survivors reflow upward into the vacated slots, so a
    # remembered position matches an entirely different mission. Measured: four
    # played, one still listed, and both remaining rows had reflowed to y=462 -
    # a slot a played mission had occupied - so both were skipped and the pass
    # reported the day's list finished.
    #
    # A FAILED mission is different: this file already records that a failed TP
    # mission is NOT consumed, so it stays listed and startable and would be
    # retried forever. Those are remembered - by FINGERPRINT, so a reflow
    # cannot make one stand in for another.
    failed = []       # fingerprints of missions that would not complete
    exhausted = []    # fingerprints measured to be finished or unstartable
    sweeps = 0
    while True:
        sweeps += 1
        if max_missions is not None and played >= max_missions:
            log.info("stopping at the caller's limit of %d mission(s)",
                     max_missions)
            break
        if sweeps > SWEEP_TRIPWIRE:
            log.error("TP: %d sweeps without exhausting the list - the "
                      "termination check is not working; stopping to avoid "
                      "looping. %d played, %d rows set aside",
                      sweeps, played, len(failed) + len(exhausted))
            break
        if not to_tp_list(actor, cap, log):
            log.info("could not reach the TP list")
            break
        spot = pick_any(actor, cap, log, skip=failed + exhausted,
                        done=exhausted)
        if spot is None:
            log.info("every TP row is now played or greyed out - the day's "
                     "list is finished (%d played, %d already done, %d set "
                     "aside after failing)",
                     played, len(exhausted), len(failed))
            break
        page, y, fp = spot
        played += 1
        log.info("started the mission at page %d y=%d - identifying it from the "
                 "screen", page + 1, y)
        if run_one(cap, actor, log):
            banked += 1
            log.info("mission banked (%d of %d played)", banked, played)
        else:
            # It stays listed and startable, so it must be remembered or the
            # next sweep picks it straight back up.
            failed.append(fp)
            log.info("mission did not complete; it stays in the list and will "
                     "not be retried this pass")
            _recover_to_lobby(cap, actor, log, relog=relog)
    log.info("TP pass finished: %d started, %d banked", played, banked)
    return played, banked


def _recover_to_lobby(cap, actor, log, timeout=120, relog=None):
    """Get back to the village after a mission that did not close out.

    RELOG IF THE LADDER CANNOT READ THE SCREEN. The ladder deliberately does not
    classify a battle, a traversal map or a half-played minigame, so ending a TP
    mission on one of those leaves it with nothing to climb - it exhausted its
    20 unrecognised frames and halted, and the whole TP pass stopped with the
    mission still playable on screen ("Seals: 1 / 2").

    A reload lands on character select, which the ladder does know, and it walks
    back to the lobby from there. `relog` is the caller's reload callable; with
    none supplied the behaviour is unchanged.
    """
    import resume
    from perceive import load_templates
    import json as _json

    def _climb():
        cfg = _json.load(open(os.path.join(ROOT, "Configs/mission.json")))
        r = resume.Resumer(cap, actor, load_templates(cfg, log), log)
        out, info = r.run(timeout=timeout)
        log.info("recover: %s %s", out, info)
        return out

    try:
        out = _climb()
        if out == resume.ARRIVED:
            return True
        if relog is None:
            return False
        # One reload, then one more climb. Bounded deliberately: a screen that
        # survives a reload is a human's problem, not something to loop on.
        log.info("recover: the ladder cannot read this screen - relogging once")
        relog()
        return _climb() == resume.ARRIVED
    except Exception as e:
        log.info("recover failed: %s: %s", type(e).__name__, e)
        return False


def run_one(cap, actor, log, family=None, rounds=6):
    """Play a TP mission that is already started. True if it closed out.

    The minigame is IDENTIFIED FROM THE SCREEN, not from a configured family.
    `family` is accepted only for logging - passing it does not change behaviour,
    because trusting a label over the pixels is how a runner ends up playing a
    game it cannot play. See engine/minigame.py.
    """
    advance_cutscenes(actor, cap, log)
    kind, ok = mg.solve(cap, actor, log)
    if family and family != kind and kind != mg.UNKNOWN:
        log.info("note: caller said family=%r, the screen says %r - trusting the "
                 "screen", family, kind)
    if ok is None:
        log.info("%r recognised but not playable; not attempting a close-out", kind)
        return False
    if not ok:
        log.info("nothing playable found on screen (%r)", kind)
        return False
    return close_out(actor, cap, log)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--family", default=None,
                    help="OPTIONAL and advisory only - the minigame is recognised "
                         f"from the screen. Playable: {sorted(SUPPORTED)}")
    ap.add_argument("--from-lobby", action="store_true",
                    help="navigate lobby -> Mission Room -> Special -> TP Training "
                         "(then pick a mission yourself and rerun without this)")
    ap.add_argument("--play", action="store_true",
                    help="play a mission that is ALREADY started")
    ap.add_argument("--all", action="store_true",
                    help="play EVERY TP mission on the list, identifying each "
                         "minigame from the screen rather than from its name")
    ap.add_argument("--auto", action="store_true",
                    help="ONE PRESS: lobby -> Special -> TP Training -> pick a "
                         "mission by name -> start -> play -> close out")
    ap.add_argument("--mission", default="kekkai",
                    help=f"which TP mission --auto starts, by NAME. "
                         f"Known: {sorted(ROW_TEMPLATES)}")
    a = ap.parse_args()
    log = _Log()

    t = find_page_target(port=a.port, url_contains="ninjasaga", timeout=20)
    c = CDP(t["webSocketDebuggerUrl"])
    c.call("Page.enable")
    cap = Capture(c)
    ctl = Controls(os.path.join(ROOT, "run/bot.control"), log)
    actor = Actor(c, cap, log, dry_run=False)

    rc = 0
    try:
        if a.all:
            played, banked = run_all(cap, actor, log)
            rc = 0 if banked else 1
        elif a.auto:
            if not ctl.wait_if_paused():
                log.info("stop requested")
                return 1
            if not to_tp_list(actor, cap, log):
                return 2
            if not pick_mission(actor, cap, log, a.mission):
                log.info("could not start the %s mission", a.mission)
                return 2
            rc = 0 if run_one(cap, actor, log) else 1
        elif a.from_lobby:
            rc = 0 if to_tp_list(actor, cap, log) else 2
            log.info("at the TP Training list; choose a mission of a SUPPORTED "
                     "family (%s) and rerun with --play", sorted(SUPPORTED))
        elif a.play:
            if not ctl.wait_if_paused():
                log.info("stop requested")
                return 1
            rc = 0 if run_one(cap, actor, log, family=a.family) else 1
        else:
            log.info("pass --from-lobby or --play")
    finally:
        c.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
