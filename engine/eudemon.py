#!/usr/bin/env python3
"""Eudemon Garden — the boss ladder, farmed to zero with a blacklist.

WHAT IT IS
----------
Village -> `Hunting House` label -> submenu -> `Eudemon Garden`. A paged list
of bosses, each row carrying a NAME, a `Level:N`, a RANK badge and an
attempts counter `x N`. Right pane previews the boss and its reward; the
bottom has `Material Market` and `Battle`.

Measured on this account - 14 bosses over 3 pages:

    SS  Izo · Kyunoki's Right Hand & … · Mudo & Kyo · Kojima     Level:1
    C   Kamaitachi 10 · Hell Horse 20
    B   Kabutomushi Musha · Kinkaku & Ginkaku
    A   Thunder Eagle 40 · Mammoth King 50
    S   Oceans Queen 55 · Ghost Soldier 60 · Battle Angel 70 · Infernal Chimera

There are exactly TEN non-SS bosses, which is almost certainly why the
reference bot's `EudemonBossSequence` was `Boss1..Boss10`: their ten were the
permanent roster and SS sits on top. Their list is positional and has no
names, and their build is a different private server, so it is not portable -
the roster is read off the screen instead.

**SS IS NEVER BLACKLISTED, BY DESIGN.** The operator's rule is that the
blacklist covers only the non-SS ranks, because SS bosses are time limited.
`blacklistable()` enforces that rather than trusting the caller, so a stale
blacklist entry cannot silently skip a limited-time boss.

MEASURED GEOMETRY (captured px, the pinned 1720x720 viewport)
-------------------------------------------------------------
    name plate      343x67 at x=1033, rows y = 300, 453, 611, 769, 926
    row pitch       ~157 (measured 153, 158, 158, 157)
    rank badge      x 1330..1470, the letter drawn in the rank's own colour
    `x` glyph       x ~1498, 73x90
    count digit     x ~1583, 57x90 for "1" and 66x90 for "3"
    page arrows     prev (1215, 1095), next (1400, 1095)
    Battle          (2374, 1123)

**THE RANK IS A COLOUR, and they separate cleanly.** Measured medians over the
badge's saturated pixels:

    rank   hue    S     V     px
    SS     120   255   196   ~4000
    B      101   176   143   ~3100
    A        5   213   233   ~2600
    S       24   153   246   ~2350
    C       39   146   143   ~2100

SS and B are the closest in hue (120 vs 101) and are separated by saturation
as well - SS is fully saturated at 255 where B is 176 - so neither test
carries the decision alone. That matters more here than usual, because
mistaking B for SS would exempt a farmable boss from the blacklist, and
mistaking SS for B would let one be blacklisted.

THE COUNTER IS ADVISORY, NOT THE AUTHORITY
------------------------------------------
`x N` says how many attempts remain, and it is read where it can be. But the
digit font is its own rendering and the set starts with only what has been
seen, so an unread count must not decide anything. The AUTHORITATIVE signal is
the same one `tp.start_row` uses: press Battle and ask whether the screen
changed. An exhausted boss cannot start a fight, so a screen that does not
move is a positive reading of "this one is finished" - not an inference from a
digit nobody could read.

That also sidesteps a question this module cannot answer from one look: the
counters read `x1` for SS and `x3` for the rest TODAY, and whether those are
daily maxima or today's remainder is unknown until a full cycle is watched.
Nothing here depends on knowing.
"""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import tp  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGIT_DIR = os.path.join(ROOT, "ref/auto/eudemon/count_digits")

# --- geometry -------------------------------------------------------------
ROW_Y = (300, 453, 611, 769, 926)
ROW_H = 67
PLATE_X = (1033, 1376)
BADGE_X = (1376, 1460)
COUNT_X = (1560, 1660)
COUNT_H = (70, 105)

PAGE_PREV = (1215, 1095)
PAGE_NEXT = (1400, 1095)
BATTLE_XY = (2374, 1123)
CLOSE_XY = (2555, 248)

# Village -> submenu -> Eudemon Garden.
VILLAGE_HH_LABEL = (1388, 722)
SUBMENU_EUDEMON = (1754, 574)
SUBMENU_SETTLE = 10.0

# --- ranks ----------------------------------------------------------------
# (hue, saturation) centres, and the tolerance each needs. See the note above
# on why SS and B need saturation as well as hue.
RANKS = (
    ("SS", 120, 255, 10, 40),
    ("B",  101, 176, 10, 45),
    ("A",    5, 213, 8,  50),
    ("S",   24, 153, 7,  45),
    ("C",   39, 146, 8,  45),
)
BADGE_MIN_PX = 800
SS = "SS"

_EX = None


def exemplars():
    global _EX
    if _EX is None:
        _EX = {}
        for p in sorted(glob.glob(os.path.join(DIGIT_DIR, "*.png"))):
            d = os.path.basename(p)[0]
            if d.isdigit():
                im = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
                if im is not None:
                    _EX.setdefault(d, []).append(
                        cv2.resize(im, (32, 44),
                                   interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0)
    return _EX


def rank_at(frame, y):
    """The rank letter's name for the row whose plate starts at `y`, or None."""
    h, w = frame.shape[:2]
    y0, y1 = max(0, y), min(h, y + ROW_H + 6)
    x0, x1 = max(0, BADGE_X[0]), min(w, BADGE_X[1])
    if y1 - y0 < 8 or x1 - x0 < 8:
        return None
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    m = hsv[:, :, 1] > 90
    if int(m.sum()) < BADGE_MIN_PX:
        return None
    hue = float(np.median(hsv[:, :, 0][m]))
    sat = float(np.median(hsv[:, :, 1][m]))
    best, best_d = None, None
    for name, h_c, s_c, h_tol, s_tol in RANKS:
        if abs(hue - h_c) > h_tol or abs(sat - s_c) > s_tol:
            continue
        d = abs(hue - h_c) / max(1.0, h_tol) + abs(sat - s_c) / max(1.0, s_tol)
        if best_d is None or d < best_d:
            best, best_d = name, d
    return best


def count_at(frame, y):
    """Attempts remaining for the row at `y`, or None if it did not read.

    None is not zero and must never be treated as such - see the module note:
    the counter is advisory and Battle is the authority.
    """
    h, w = frame.shape[:2]
    y0, y1 = max(0, y - 15), min(h, y + ROW_H + 25)
    x0, x1 = max(0, COUNT_X[0]), min(w, COUNT_X[1])
    if y1 - y0 < 8 or x1 - x0 < 8:
        return None
    g = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    m = (g < 90).astype(np.uint8) * 255
    n, lab, st, _ce = cv2.connectedComponentsWithStats(m)
    ex = exemplars()
    if not ex:
        return None
    digits = []
    for i in range(1, n):
        x, yy, bw, bh, a = st[i]
        if a < 400 or not (COUNT_H[0] <= bh <= COUNT_H[1]) or bw > 90:
            continue
        digits.append((int(x), (lab[yy:yy + bh, x:x + bw] == i).astype(np.uint8) * 255))
    if not digits:
        return None
    digits.sort()
    text = ""
    for _x, crop in digits:
        q = cv2.resize(crop, (32, 44),
                       interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        scored = sorted((float(np.abs(q - e).mean()), d)
                        for d, es in ex.items() for e in es)
        best_d, best = scored[0]
        second = next((d for d, lb in scored if lb != best), None)
        margin = (second / best_d) if best_d > 1e-6 else 1e9
        if best_d > 0.13 or margin < 1.8:
            return None
        text += best
    return int(text) if text else None


FP_W, FP_H = 48, 10
FP_TOL = 6.0


def row_fingerprint(frame, y):
    """A cheap fingerprint of the NAME PLATE at `y`. None if unsampleable."""
    fh, fw = frame.shape[:2]
    y0, y1 = max(0, y), min(fh, y + ROW_H)
    x0, x1 = max(0, PLATE_X[0]), min(fw, PLATE_X[1])
    if y1 - y0 < 4 or x1 - x0 < 4:
        return None
    g = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (FP_W, FP_H), interpolation=cv2.INTER_AREA)


def same_row(a, b, tol=FP_TOL):
    if a is None or b is None or getattr(a, "shape", None) != getattr(b, "shape", None):
        return False
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()) <= tol


# The plate WIDTH varies with the boss name - measured 325..425, because a
# long name merges the plate with the art beside it. Height and x are the
# stable part, so the width window is generous and the pitch does the work.
PLATE_W = (300, 470)
PLATE_H = (50, 90)
PLATE_MIN = 3          # a page always shows at least four rows
PLATE_PITCH = (140, 175)
PLATE_PITCH_MAX_SKIP = 3   # a row may fail to segment; allow a multiple


def plates(frame):
    """The white NAME PLATES, top to bottom - this is the garden's anchor.

    **A RANK COLOUR IS NOT AN ANCHOR.** The first version assumed the five row
    positions and read a rank at each, and matched 60 of 113 non-garden
    reference frames - combat, the lobby, the village. Saturated art is
    everywhere; a hue window over a small box proves nothing about what screen
    this is. So the LIST is found first, structurally, and ranks are only read
    at rows that a plate was actually found on.

    Measured: 343x67 at x=1033, evenly pitched (153, 158, 158, 157).
    """
    fh, fw = frame.shape[:2]
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    m = ((g > 225).astype(np.uint8) * 255)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 25), np.uint8))
    n, _lab, st, _ce = cv2.connectedComponentsWithStats(m)
    found = []
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if not (PLATE_W[0] <= w <= PLATE_W[1]):
            continue
        if not (PLATE_H[0] <= h <= PLATE_H[1]):
            continue
        if abs(int(x) - PLATE_X[0]) > 60:
            continue
        found.append(int(y))
    found.sort()
    if len(found) < PLATE_MIN:
        return []
    # EVENLY PITCHED, or it is not a list. Anything else that happens to draw
    # three pale bars is rejected here rather than by a threshold nudge.
    # EVENLY PITCHED, or it is not a list. A gap may be a MULTIPLE of the
    # pitch when one plate fails to segment, so multiples are accepted rather
    # than loosening the window - which would let arbitrary pale bars through.
    def _spaced(gp):
        for k in range(1, PLATE_PITCH_MAX_SKIP + 1):
            if PLATE_PITCH[0] * k <= gp <= PLATE_PITCH[1] * k:
                return True
        return False

    gaps = [b - a for a, b in zip(found, found[1:])]
    if gaps and not all(_spaced(gp) for gp in gaps):
        return []
    return found


def in_garden(frame):
    return bool(plates(frame))


def rows(frame):
    """Every visible row: y, rank, count, and a reflow-safe fingerprint.

    The fingerprint is keyed on a row's PIXELS, so a blacklist survives the
    list being re-paged or re-ordered where an index would not.

    **It cannot be `tp.row_fingerprint`.** That one samples x 1700..2500,
    which on a TP list is the mission row's own right-hand side but HERE is
    the boss PREVIEW PANE - shared by every row and repainted on selection. It
    would hand back the same fingerprint for all five rows on a page, so a
    single blacklist entry would silently skip the lot. This samples the name
    plate instead, which is what actually differs per row.
    """
    out = []
    for i, y in enumerate(plates(frame)):
        out.append({"index": i, "y": y, "rank": rank_at(frame, y),
                    "count": count_at(frame, y),
                    "fp": row_fingerprint(frame, y)})
    return out


def row_key(rank, n):
    """A short, stable label for the panel: `SS-1`, `A-2`, ..."""
    return "%s-%d" % (rank, n)


def harvest_roster(frame, roster):
    """Add any unseen row to `roster` (a list of dicts). Returns it.

    Keys are assigned in DISCOVERY ORDER and matched back by FINGERPRINT, not
    by hashing one. A hash of a pixel fingerprint changes whenever any pixel
    does, which is the opposite of what a stable identity needs; `same_row`
    already compares with tolerance and is what the rest of this module uses.
    """
    for r in rows(frame):
        if r["rank"] is None or r["fp"] is None:
            continue
        if any(same_row(r["fp"], e["fp"]) for e in roster):
            continue
        n = sum(1 for e in roster if e["rank"] == r["rank"]) + 1
        roster.append({"key": row_key(r["rank"], n), "rank": r["rank"],
                       "fp": r["fp"]})
    return roster


def blacklistable(rank):
    """SS is never blacklistable - those bosses are time limited."""
    return rank is not None and rank != SS


# --- navigation -----------------------------------------------------------
def to_garden(actor, cap, log):
    """Village -> Hunting House submenu -> Eudemon Garden. True if it opened."""
    tp.game_scroll(cap, 0.0)
    time.sleep(0.4)
    actor.click_pixel(*VILLAGE_HH_LABEL, why="village: Hunting House label")
    time.sleep(2.5)
    actor.click_pixel(*SUBMENU_EUDEMON, why="submenu: Eudemon Garden")
    time.sleep(SUBMENU_SETTLE)
    if not rows(cap.frame(gray=False)):
        log.info("eudemon: the garden did not open")
        return False
    return True


def close(actor, cap, log, tries=4):
    """Back to the village."""
    from perceive import find
    lobby = tp._tpl("lobby_rail_fortune")
    for _ in range(tries):
        g = cv2.cvtColor(cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
        m, conf = find(g, lobby)
        if m.found:
            return True
        actor.click_pixel(*CLOSE_XY, why="close Eudemon Garden")
        time.sleep(2.2)
    return False


def goto_page(actor, cap, log, page, max_pages=3):
    """Page 1 is reached by pressing prev until it stops moving."""
    for _ in range(max_pages):
        actor.click_pixel(*PAGE_PREV, why="Eudemon list: first page")
        time.sleep(1.6)
    for _ in range(max(0, page - 1)):
        actor.click_pixel(*PAGE_NEXT, why="Eudemon list: next page")
        time.sleep(1.8)
    return rows(cap.frame(gray=False))


# --- starting a fight -----------------------------------------------------
# `tp.start_row` reads "did the screen change" to tell a startable row from an
# exhausted one, calibrated at 0.02 against a 0.0004 flicker. The same
# question is asked here, for the same reason - see the module note.
START_GATE = 0.02
SELECT_SETTLE = 1.6
BATTLE_SETTLE = 4.0


def _changed(a, b):
    if a is None or b is None or a.shape != b.shape:
        return 1.0
    return float(np.mean(cv2.absdiff(a, b))) / 255.0


def start(actor, cap, log, row):
    """Select a row and press Battle. (started, reason).

    "exhausted" is a POSITIVE reading: a boss with no attempts left cannot
    start a fight, so a screen that does not move says so. It is not inferred
    from a counter, which may not have read at all.
    """
    actor.click_pixel((PLATE_X[0] + PLATE_X[1]) // 2, row["y"] + ROW_H // 2,
                      why=f"eudemon: select the {row['rank']} boss")
    time.sleep(SELECT_SETTLE)
    before = cap.frame(gray=False)
    actor.click_pixel(*BATTLE_XY, why="eudemon: Battle")
    time.sleep(BATTLE_SETTLE)
    after = cap.frame(gray=False)
    d = _changed(before, after)
    if d < START_GATE:
        log.info("eudemon: the %s boss did not start (screen moved %.4f) - "
                 "reading that as no attempts left", row["rank"], d)
        return False, "exhausted"
    if rows(after):
        log.info("eudemon: still on the list after Battle (moved %.4f)", d)
        return False, "transient"
    return True, "started"


VILLAGE_RECRUIT = (1078, 913)      # the village's "Recruit Friends" plaque
PANEL_CLOSE = (2597, 192)
RECRUIT_SETTLE = 3.0


def recruit_party(cap, actor, cdp, log, want=2):
    """Fill the party from the village, then close the panel. Levels taken.

    **TEAMMATES LEAVE AFTER EVERY BOSS** - the recruit panel says so outright
    ("Teammates will leave your group after each mission or boss") - so this
    belongs INSIDE the loop, before each fight, not once at the start.

    A failure here is not fatal: a party is help, not a precondition, and a
    boss can be attempted solo. It is logged and the hunt goes on.
    """
    if cdp is None:
        return []
    try:
        import roster
        actor.click_pixel(*VILLAGE_RECRUIT, why="village: Recruit Friends")
        time.sleep(RECRUIT_SETTLE)
        took = roster.recruit(cap, actor, cdp, log, want=want)
        actor.click_pixel(*PANEL_CLOSE, why="close the team panel")
        time.sleep(2.0)
        return took
    except Exception as e:
        log.warning("eudemon: recruiting failed (%s: %s) - fighting with "
                    "whoever is already in the party",
                    type(e).__name__, e)
        return []


def hunt(cap, actor, log, play_combat, blacklist=(), max_fights=40,
         relog=None, skip_ss=False, cdp=None, recruit=True,
         roster=None, skip_keys=(), on_roster=None):
    """Farm every non-blacklisted boss until nothing will start.

    `blacklist` holds row FINGERPRINTS (see `rows`). SS rows are never
    skipped by it - `blacklistable` decides that, not the caller - because SS
    bosses are time limited and a stale entry must not cost one.
    """
    fought = banked = 0
    exhausted = list(blacklist or [])
    for sweep in range(max_fights):
        # RECRUIT FIRST, FROM THE VILLAGE, and before choosing a target.
        # The party empties after every boss - the panel says so outright -
        # so this belongs inside the loop. Doing it before the search matters:
        # an earlier version recruited after picking a target and then re-found
        # that row on PAGE 1 only, so a target from page 2 or 3 kept a stale y
        # and the next click would have landed on the wrong boss.
        if recruit and cdp is not None and close(actor, cap, log):
            took = recruit_party(cap, actor, cdp, log)
            if took:
                log.info("eudemon: party filled with Lv%s",
                         ", Lv".join(str(t) for t in took))

        if not in_garden(cap.frame(gray=False)):
            if not to_garden(actor, cap, log):
                log.info("eudemon: cannot reach the garden - stopping")
                break

        target = None
        for page in (1, 2, 3):
            found = goto_page(actor, cap, log, page)
            if roster is not None:
                before = len(roster)
                harvest_roster(cap.frame(gray=False), roster)
                if on_roster and len(roster) != before:
                    on_roster(roster)
            log.info("eudemon: page %d has %d row(s): %s", page, len(found),
                     [f"{r['rank']}x{r['count']}" for r in found])
            for r in found:
                if any(same_row(r["fp"], fp) for fp in exhausted):
                    continue
                if skip_ss and r["rank"] == SS:
                    continue
                # THE PANEL'S SKIP LIST. Matched by fingerprint against the
                # harvested roster, and SS is exempt whatever the list says -
                # `blacklistable` is the single place that rule lives.
                if roster is not None and skip_keys:
                    hit = next((e for e in roster
                                if same_row(r["fp"], e["fp"])), None)
                    if hit and hit["key"] in skip_keys \
                            and blacklistable(r["rank"]):
                        continue
                # A COUNT OF ZERO IS NO TRIES LEFT. It is only trusted when it
                # actually READ - `count_at` returns None where it could not,
                # and None is not zero. The authority stays `start`, which
                # presses Battle and asks whether the screen moved.
                if r["count"] == 0:
                    log.info("eudemon: the %s boss reads x0 - no tries left",
                             r["rank"])
                    exhausted.append(r["fp"])
                    continue
                target = r
                break
            if target:
                break
        if target is None:
            log.info("eudemon: every boss is blacklisted or finished "
                     "(%d fought, %d banked)", fought, banked)
            break

        started, why = start(actor, cap, log, target)
        if not started:
            if why == "exhausted":
                exhausted.append(target["fp"])
            continue
        fought += 1
        log.info("eudemon: fight %d begins (%s boss)", fought, target["rank"])
        try:
            play_combat()
        except Exception as e:
            log.warning("eudemon: the battle runner raised %s: %s",
                        type(e).__name__, e)
        # WIN OR LOSE, GET BACK TO THE LOBBY. close_out banks a win; whether
        # it banked or not, the lap ends in the village so the next one can
        # recruit and start again.
        if close_out(actor, cap, log):
            banked += 1
            log.info("eudemon: banked (%d of %d fought)", banked, fought)
        else:
            log.info("eudemon: that fight did not bank")
        if not close(actor, cap, log):
            log.info("eudemon: could not get back to the village")
            if relog:
                relog()
    return fought, banked

# --- the reward panel -----------------------------------------------------
#
# **A EUDEMON WIN DOES NOT LOOK LIKE A MISSION SUCCESS.** The farm and TP
# banner is a wide landscape panel dismissed by a GREEN CHECK; a Eudemon boss
# pays out on a TALL PORTRAIT panel dismissed by a RED X, and it carries a
# `Share` button. Measured on a live win (`Izo`, XP 45,650 / Gold 45,650 plus
# a materials drop):
#
#     mission_success   0.266      <- the farm banner does not match at all
#     result_panel      0.524
#     mission_start     0.668      <- the green check is not on this panel
#     close_popup_x     0.951      <- the X that dismisses it, at (2132, 242)
#
# So `tp.close_out` cannot bank one: it waits for a check that is not there,
# and the fight was reported `stalled` after a 90 s turn-gate timeout on a
# mission that had been WON. The bot sat on the reward screen.
#
# **NEVER PRESS `Share`.** It publishes to a social feed, which this project
# forbids doing unasked - the same rule as the TP "Share to wall" dialog. The
# X is located BY TEMPLATE and the click is additionally required to be in the
# panel's top-right, so a mis-match cannot wander onto the green button.
REWARD_X_MIN_Y = 120
REWARD_X_MAX_Y = 420
REWARD_X_MIN_X = 1900
REWARD_CLEARED_SETTLE = 2.5


def reward_panel(frame):
    """(x, y) of the reward panel's close X, or None.

    Constrained to the panel's top-right corner. `close_popup_x` is a generic
    template used on several screens, and an unconstrained match on a panel
    that also carries a Share button is exactly the sort of loose targeting
    this project has been bitten by.
    """
    from perceive import find
    # THE GARDEN HAS ITS OWN CLOSE X, in the same corner - `close_popup_x`
    # matched all three list pages. A reward panel is never the list, and
    # `plates()` is a positive reading of the list, so this costs nothing and
    # removes the whole class of confusion.
    if plates(frame):
        return None
    t = tp._tpl("close_popup_x", 0.85)
    if t is None:
        return None
    m, _c = find(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), t)
    if not m.found:
        return None
    x, y = m.center
    if not (REWARD_X_MIN_Y <= y <= REWARD_X_MAX_Y) or x < REWARD_X_MIN_X:
        return None
    return (int(x), int(y))


def close_out(actor, cap, log, timeout=45):
    """Acknowledge a Eudemon reward panel. True once it is gone.

    Returns True only when the panel actually clears - the same rule the rest
    of this project uses, that a reward is not banked until the screen moves
    on.
    """
    t0 = time.time()
    seen = False
    while time.time() - t0 < timeout:
        f = cap.frame(gray=False)
        xy = reward_panel(f)
        if xy is None:
            if seen:
                log.info("eudemon: reward panel acknowledged - banked")
                return True
            if in_garden(f):
                return False        # never opened a panel; nothing to bank
            time.sleep(1.0)
            continue
        seen = True
        actor.click_pixel(*xy, why="eudemon: close the reward panel (X, "
                                   "never Share)")
        time.sleep(REWARD_CLEARED_SETTLE)
    log.info("eudemon: the reward panel did not clear in %ds", timeout)
    return False
