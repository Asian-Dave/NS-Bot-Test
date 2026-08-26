#!/usr/bin/env python3
"""Recognise which TP minigame is on screen, and dispatch to its solver.

WHY THIS REPLACES A `--family` FLAG
-----------------------------------
The first version of the TP runner took `--family kekkai` and trusted it. That is
the fixed-script anti-pattern CLAUDE.md warns against —

    "Branch on observed state; never follow a fixed script."

and it is also fragile in a specific way: a mislabelled mission would start a
minigame the runner could not play and burn a life, or worse, click blindly on an
unrecognised screen. So the family is now READ OFF THE SCREEN. The mission name
is only used to avoid *starting* something we know we cannot finish; once a
minigame is open, what it actually is decides what happens.

WHAT IT CAN TELL APART
----------------------
    "kekkai"      the rune Mastermind. Either an unsealed kekkai standing in the
                  scene, or its puzzle panel already open.  -> SOLVABLE
    "seal_entry"  the hand-seal minigame: `Skill : N / 4`, three hearts, a named
                  target jutsu, two empty slots, ten face-up hand seals.
                  -> NOT SOLVABLE YET, and it says so instead of guessing.
    "unknown"     anything else - cutscene, traversal, a panel, the lobby.

    "combat"      a battle, not a minigame - handed back to the battle runner.

Every detection is measured, not guessed:

    tp_seal_hud ("Skill :" label)   1.000 positive, 0.268..0.348 on all others
    kekkai seal in scene            area >= 8000, h >= 200, 0.9 <= aspect <= 2.0,
                                    fill <= 0.45
    kekkai puzzle panel             >= 5 green history discs
    combat                          charge_btn + dodge_btn via BattleGeometry

WHY seal_entry IS NOT SOLVABLE, AND WHAT WOULD FIX IT
-----------------------------------------------------
The game shows a jutsu name (e.g. "Earth Strangle") and ten hand seals, and you
must pick TWO in the right order. The answer is not on screen:

* the two slots are card BACKS - they are the empty input, not a revealed answer
* a 47 fps burst over 5 s across the whole slot strip caught no reveal, so
  CLAUDE.md's "revealed briefly after Start" hypothesis does not hold at that
  sampling rate
* the mapping is not in the client we have. CLAUDE.md already records that
  per-skill data lives in a runtime-populated, server-fed `SKILL_DATA`, and a
  string search of the shell SWF finds no jutsu names and no seal vocabulary

Brute force is out: ten seals in two ordered slots is 90 possibilities against
THREE hearts, and a miss also rerolls the target jutsu, so you cannot even
narrow one skill down across attempts.

So it needs a jutsu -> seal-pair TABLE, harvested once. The plausible source is
the game's own Jutsu panel, which should list each jutsu with its seals; that is
a separate offline harvesting job, not something to attempt mid-minigame with
three lives on the line. Until that table exists, `solve()` declines.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2

import kekkai_play as kp
from perceive import Template, find

KEKKAI = "kekkai"
SEAL_ENTRY = "seal_entry"
CARDS = "cards"
COMBAT = "combat"
UNKNOWN = "unknown"

# `cards` now has a calibrated match gate AND confirms every pair against the
# board, but it is a TIMED game that has not yet been completed end to end, so it
# stays separate from the fully-trusted kekkai until a live run banks a reward.
SOLVABLE = {KEKKAI}
EXPERIMENTAL = {CARDS}


def _tpl(name, thr=0.88):
    p = os.path.join(ROOT, "tpl", f"{name}.png")
    return Template(name, p, threshold=thr) if os.path.exists(p) else None


def classify(frame):
    """Which minigame is on screen. Returns (kind, evidence dict).

    Ordered most-specific first. Two orderings matter:

    * The seal HUD goes first because its "Skill :" label is unambiguous
      (1.000 against 0.268..0.348 on every other frame we hold).
    * COMBAT is checked BEFORE the kekkai scene search, and that is not
      cosmetic. The battle target ring is red, wide, tall and sparse - measured
      area 11988, bbox 395x264, fill 0.115, aspect 1.50 - which passes every
      shape filter a real kekkai passes. Shape CANNOT separate them, so context
      has to: if the command bar is on screen we are fighting, not sealing.
      Detecting combat reuses `BattleGeometry`, which is already validated across
      three canvas geometries.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    ev = {}

    hud = _tpl("tp_seal_hud")
    if hud is not None:
        m, c = find(gray, hud)
        ev["seal_hud"] = round(c, 3)
        if m.found:
            return SEAL_ENTRY, ev

    ch_hud = _tpl("tp_cards_hud")
    if ch_hud is not None:
        m, c = find(gray, ch_hud)
        ev["cards_hud"] = round(c, 3)
        if m.found:
            return CARDS, ev

    ch, do = _tpl("charge_btn", 0.70), _tpl("dodge_btn", 0.70)
    if ch is not None and do is not None:
        from geometry import BattleGeometry
        geo = BattleGeometry.locate(gray, ch, do)
        if geo is not None:
            return COMBAT, dict(ev, command_bar=round(geo.confidence, 3),
                                scale=geo.scale)

    gx, ys = kp.find_rows(frame)
    ev["history_rows"] = len(ys)
    if gx is not None:
        return KEKKAI, dict(ev, where="puzzle panel open")

    seal = kp.find_kekkai(frame)
    ev["seal_in_scene"] = seal
    if seal:
        nodes = kp.count_nodes(frame, seal)
        return KEKKAI, dict(ev, where="seal in scene", nodes=nodes)

    return UNKNOWN, ev


def solve(cap, actor, log, frame=None):
    """Identify what is on screen and play it. Returns (kind, ok).

    `ok` is None when the minigame is recognised but unsupported — which is a
    third outcome, distinct from success and from failure, and the caller should
    treat it as "leave this alone" rather than "retry".
    """
    frame = frame if frame is not None else cap.frame(gray=False)
    kind, ev = classify(frame)
    log.info("minigame: %s %s", kind, ev)

    if kind == KEKKAI:
        solved = kp.hunt_and_solve(cap, actor, log)
        return kind, bool(solved)

    if kind == CARDS:
        import cards as cd
        log.info("memory pair-matching board; matching by 3x4 mean-HSV signature "
                 "(calibrated worst true pair 4.56 vs best non-pair 8.99) and "
                 "confirmed cell-by-cell against the board itself, so a cleared "
                 "count is the game's verdict rather than the metric's.")
        pairs, el = cd.play(cap, actor, log, save_crops=True)
        return kind, pairs > 0

    if kind == SEAL_ENTRY:
        log.info("seal-entry minigame recognised, and it is NOT solvable yet: it "
                 "needs a jutsu -> seal-pair table that is not in the client "
                 "(SKILL_DATA is server-fed). Ten seals in two ordered slots is "
                 "90 options against three hearts, and a miss rerolls the jutsu, "
                 "so guessing would just spend the lives. Leaving it untouched.")
        return kind, None

    if kind == COMBAT:
        log.info("this is a battle, not a minigame - leaving it to the battle "
                 "runner")
        return kind, None

    log.info("no minigame recognised on this screen")
    return kind, False


def main():
    import argparse
    from capture import Capture
    from cdp import CDP, find_page_target

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--frames", nargs="*",
                    help="classify saved frames instead of the live screen")
    a = ap.parse_args()

    if a.frames:
        for p in a.frames:
            f = cv2.imread(p)
            if f is None:
                print(f"  {p}: unreadable")
                continue
            kind, ev = classify(f)
            print(f"  {os.path.basename(p):34s} -> {kind:11s} {ev}")
        return 0

    t = find_page_target(port=a.port, url_contains="ninjasaga", timeout=20)
    c = CDP(t["webSocketDebuggerUrl"])
    c.call("Page.enable")
    kind, ev = classify(Capture(c).frame(gray=False))
    print(f"live screen -> {kind}  {ev}")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
