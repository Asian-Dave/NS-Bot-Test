#!/usr/bin/env python3
"""Story-mission farming — choose the best mission available, then play it.

WHAT THE OPERATOR ASKED FOR, AND WHAT THAT MEANS
------------------------------------------------
    "if in the mission panel just continue to navigate, if in the lobby just
     click Mission Room. Some config to pre-select a mission to farm in a loop,
     else farm the highest one available - C to A depending what is unlocked;
     greyed out means unavailable, and locked rows are not available either."

So there are two selectors, and they are different questions:

  * WHICH GRADE - S / A / B / C, chosen once per run. Locked grades are out.
  * WHICH MISSION inside that grade - the highest level our character can
    actually start, which means the last row that is not padlocked.

Both default to "the best available" and both can be pinned in config.

GRADES ARE READ BY COLOUR, NOT BY TEXT
--------------------------------------
The grade panel draws four bars, and they are colour-coded. Measured on the live
panel, taking only strongly saturated pixels (S > 110):

    Grade A   hue ~103   blue
    Grade B   hue ~51    green
    Grade C   hue ~128   purple
    Grade S   never saturated - it renders GREY under a padlock

That last line is the useful part: a locked grade cannot produce a saturated
stripe, so "is this grade available" and "which grade is this" fall out of the
same measurement. Text templates would need one crop per grade and would still
have to answer the locked question separately.

The white label box covers the middle of each bar, so the saturated stripe is
the bar's lower edge. Clicking the stripe is still inside the bar.

WHY GRADE CHOICE MATTERS MORE THAN ANYTHING ELSE HERE
-----------------------------------------------------
CLAUDE.md records the measurement: Grade C page 1 yields 20 XP, Grade A page 1
yields 4,870. Picking the wrong grade does not fail the run, it just wastes it -
which is exactly why `mission.grade` had no default and the bot refused to guess.
Reading the panel removes the need to guess at all.

LOCKED ROWS ARE INERT, NOT SLOW
-------------------------------
Grade A spans Lv 42-78 in +2 steps. Rows above the character's level render
greyed with a padlock and DO NOTHING when clicked - the bot dead-loops on them
rather than erroring. `mission_locked` is therefore required, and the highest
startable mission is the LAST row that is not padlocked.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

import tp
from perceive import Template, find

# hue -> grade, on OpenCV's 0..179 scale, measured on the live panel.
GRADE_HUES = (("A", 95, 115), ("B", 40, 65), ("C", 120, 140))
# Best first. S is included so that if it is ever unlocked it wins automatically.
GRADE_ORDER = ("S", "A", "B", "C")
SAT_MIN = 110.0            # a lit grade bar; a locked one never reaches this
BAND_FRAC = 0.50           # of the scanned width


def _tpl(name, thr=0.88):
    p = os.path.join(ROOT, "tpl", f"{name}.png")
    return Template(name, p, threshold=thr) if os.path.exists(p) else None


def find_grades(frame, x0=1800, x1=2400, y0=250, y1=1100, step=6):
    """Which grade bars are lit, and where. {grade: (x, y)}.

    A locked grade is absent from the result BY CONSTRUCTION - it renders grey
    and never clears the saturation gate - so callers do not need a separate
    "is it locked" check.

    GATED ON THE PANEL BEING OPEN. Colour alone is not enough: a bare hue scan
    also finds "Grade A" in the village and "Grade B" in a battle, because both
    screens have large saturated blue and green areas. Measured, `grade_tab`
    scores 0.981 on the grade panel and 0.345..0.509 on the lobby, a battle, the
    TP list and the Special tab, so it says cleanly whether this screen is the
    one where a hue means a grade.
    """
    gt = _tpl("grade_tab")
    if gt is not None:
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if not (gt.h > g.shape[0] or gt.w > g.shape[1]) and not find(g, gt)[0].found:
            return {}
    h, w = frame.shape[:2]
    x0, x1 = max(0, x0), min(w, x1)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    runs, cur = [], []
    for y in range(max(0, y0), min(h, y1), step):
        band = hsv[y:y + step, x0:x1]
        if band.size == 0:
            continue
        s = band[:, :, 1]
        lit = s > SAT_MIN
        if float(lit.mean()) >= BAND_FRAC:
            hue = float(np.median(band[:, :, 0][lit]))
            cur.append((y, hue))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    out = {}
    for r in runs:
        ys = [y for y, _ in r]
        hue = float(np.median([hv for _, hv in r]))
        for g, lo, hi in GRADE_HUES:
            if lo <= hue <= hi and g not in out:
                out[g] = ((x0 + x1) // 2, int((ys[0] + ys[-1]) / 2))
    return out


def pick_grade(actor, cap, log, prefer=None, settle=2.4):
    """Click the best available grade. Returns the letter, or None."""
    frame = cap.frame(gray=False)
    grades = find_grades(frame)
    if not grades:
        log.info("no grade bars found - is the Story tab open?")
        return None
    log.info("grades available: %s", sorted(grades))
    if prefer:
        if prefer not in grades:
            log.info("grade %r is not available (locked, or not on screen); "
                     "available: %s", prefer, sorted(grades))
            return None
        choice = prefer
    else:
        choice = next((g for g in GRADE_ORDER if g in grades), None)
        if choice is None:
            return None
        log.info("no grade configured - taking the best available (%s). "
                 "Grade A page 1 yields 4,870 XP against Grade C's 20, so this "
                 "is the whole economics of the run.", choice)
    actor.click_pixel(*grades[choice], why=f"Grade {choice}")
    time.sleep(settle)
    return choice


def locked_rows(frame_gray, log=None):
    """Y positions of padlocked rows on the current list page."""
    t = _tpl("mission_locked")
    if t is None:
        if log:
            log.info("no mission_locked template - CANNOT tell a locked row "
                     "from a startable one, and clicking a locked row does "
                     "nothing forever. Refusing to guess.")
        return None
    from mission import _find_all
    return [p[1] for p in _find_all(frame_gray, t, max_hits=6)]


def pick_highest(actor, cap, log, max_pages=12, settle=2.4):
    """Start the highest-level startable mission in the current grade.

    Rows run low level to high, three per page, so the highest one we can
    actually play is the LAST row that is not padlocked. This pages to the end,
    remembering the last unlocked row it saw, and comes back to it.
    """
    nxt = _tpl("page_next", 0.85)
    best = None                      # (page, y)
    pages = 0
    for page in range(max_pages):
        frame = cap.frame(gray=False)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        rows = tp.find_mission_rows(frame)
        locked = locked_rows(gray, log)
        if locked is None:
            return None
        free = [y for y in rows
                if not any(abs(y - ly) < 60 for ly in locked)]
        log.info("page %d: %d row(s), %d locked, %d startable",
                 page + 1, len(rows), len(locked), len(free))
        if free:
            best = (page, free[-1])
        pages = page
        m, c = find(gray, nxt)
        if not m.found:
            break
        # Every row locked from here on means we are past our level; no point
        # paging further.
        if rows and not free and best is not None:
            log.info("every row on this page is above our level; stopping here")
            break
        actor.click_pixel(*m.center, why=f"next page ({c:.3f})")
        time.sleep(1.6)

    if best is None:
        log.info("no startable mission in this grade - every row is locked")
        return None
    page, y = best
    # Page back to where it was.
    if page < pages:
        prv = _tpl("page_prev", 0.85)
        for _ in range(pages - page):
            gray = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
            m, c = find(gray, prv) if prv is not None else (None, 0)
            if m is None or not m.found:
                log.info("no previous-page arrow; cannot return to page %d",
                         page + 1)
                return None
            actor.click_pixel(*m.center, why="previous page")
            time.sleep(1.4)
    log.info("starting the highest startable mission (page %d, y=%d)",
             page + 1, y)
    return tp.start_row(actor, cap, log, y, settle=settle)


def to_grade_panel(actor, cap, log):
    """Get to the Story grade panel from wherever we are. False if it stalls.

    Deliberately tolerant about the starting point, as asked: already on the
    panel is fine, inside the Mission Room is fine, and the lobby is fine.
    Anything else is somebody else's job - the resume ladder's.
    """
    tp.game_scroll(cap, 0.0)
    time.sleep(0.3)
    if find_grades(cap.frame(gray=False)):
        log.info("already on the grade panel")
        return True
    entry = _tpl("mission_room_entry")
    if entry is not None and tp.click_when(actor, cap, entry, "enter Mission Room"):
        if find_grades(cap.frame(gray=False)):
            return True
    # In the Mission Room but on the Special tab: the Story tab is the way back.
    story = _tpl("story_tab")
    if story is not None and tp.click_when(actor, cap, story, "Story tab"):
        return bool(find_grades(cap.frame(gray=False)))
    log.info("could not reach the grade panel")
    return bool(find_grades(cap.frame(gray=False)))


def start_best(cap, actor, log, grade=None, row=None):
    """Pick and start a mission. Returns the grade played, or None."""
    if not to_grade_panel(actor, cap, log):
        return None
    g = pick_grade(actor, cap, log, prefer=grade)
    if g is None:
        return None
    if row is not None:
        log.info("mission row pinned by config at y=%s", row)
        return g if tp.start_row(actor, cap, log, row) else None
    return g if pick_highest(actor, cap, log) else None


def farm(cap, actor, log, cfg, controls=None, repeat=0):
    """Farm story missions in a loop. Returns (started, banked)."""
    import mission as mission_mod
    from gate import Gate

    m = cfg.get("mission", {})
    grade, row = m.get("grade"), m.get("mission_row")
    started = banked = 0
    n = repeat or m.get("repeat", 0) or 0
    while True:
        if controls is not None and not controls.wait_if_paused():
            log.info("stop requested")
            break
        if n and started >= n:
            break
        g = start_best(cap, actor, log, grade, row)
        if g is None:
            log.info("nothing startable; stopping")
            break
        started += 1
        runner = mission_mod.MissionRunner(
            Gate(cap, log, controls), actor, cap,
            __import__("bot").load_templates(cfg, log), cfg, log, controls)
        runner.grade = g          # the panel already told us; do not re-require it
        try:
            out, stats = runner.run()
        except Exception as e:
            log.error("mission runner error: %s: %s", type(e).__name__, e)
            break
        log.info("mission %d finished: %s %s", started, out, stats)
        if str(out).endswith("SUCCESS") or out == "success":
            banked += 1
    log.info("farm: %d started, %d banked", started, banked)
    return started, banked
