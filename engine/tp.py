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
SUPPORTED = {"kekkai", "scroll"}

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


def click_when(actor, cap, tpl, why, tries=4, settle=2.6):
    """Match `tpl` and click it, retrying. False if never found."""
    for _ in range(tries):
        g = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
        m, c = find(g, tpl)
        if m.found:
            actor.click_pixel(*m.center, why=f"{why} ({c:.3f})")
            time.sleep(settle)
            return True
        time.sleep(1.2)
    return False


def to_tp_list(actor, cap, log):
    """lobby -> Mission Room -> Special -> TP Training. False if it stalls."""
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
}


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
    t0 = time.time()
    seen = False
    while time.time() - t0 < timeout:
        f = cap.frame(gray=False)
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
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
            log.info("Success panel cleared")
            return True
        time.sleep(1.5)
    log.info("close-out timed out after %ss", timeout)
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
        if a.auto:
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
