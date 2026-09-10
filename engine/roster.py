#!/usr/bin/env python3
"""Reading the recruit rail: who is on it, what level they are, and the team.

WHY THIS IS ITS OWN MODULE
--------------------------
Two different screens need the same three answers - the player's own level,
the level printed on each recruit card, and how full the team is - and the
recruit rail is shared by every mode that can take teammates. Putting it
beside either caller would make the other one import a stranger.

THE RULE THIS SERVES
--------------------
Recruit two party members at or below the PLAYER'S OWN level, from FRIENDS
only. The NPC cards on the same rail are token-priced, and this project's
standing rule is that tokens are never spent - so NPCs are excluded by name
AND by tab, not by one or the other.

MEASURED GEOMETRY (captured px, the pinned 1720x720 viewport)
-------------------------------------------------------------
    card pitch          192 px, exactly regular across seven cards
    "Lv" label          x = 862 + 192k, y 1375, two blobs (L and v)
    level digits        x = 900 + 192k and 921 + 192k, y 1370, ~17x25
    friend cards        k = 0..5 on this account; NPC cards follow
    tabs (bottom left)  friends = globe + 3 figures (835, 1268)
                        NPC     = two ninja faces  (955, 1260)

**THE CARDS ARE LOCATED, NOT ASSUMED.** The pitch above is what the layout
measured, and it is used only to group blobs into cards; the blobs themselves
are found by colour every time. A rail with a different number of friends, or
scrolled by its paging arrows, still reads correctly because the grouping is
derived from the blobs that are actually there.

ONE DIGIT SET FOR TWO SIZES
---------------------------
The player's own level is drawn larger than a card's (measured ~37 px tall
against ~25) and in a different arrangement - "Lv" above the number rather
than beside it. Both are bright glyphs on a dark plate, so a brightness mask
gives the same SHAPE at either size, and the exemplars are compared after
normalising to a fixed box. That is the trick `balance.py` already uses to
make one set serve its pale row numbers and its red sums.

An unreadable digit is REFUSED, never rounded to the nearest exemplar. A
misread level would recruit someone above the player, which is the one thing
the rule exists to prevent.
"""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGIT_DIR = os.path.join(ROOT, "ref/auto/hh/level_digits")

# --- the rail -------------------------------------------------------------
RAIL_BAND = (1358, 1412)          # y range holding the "Lv NN" badges
RAIL_X = (760, 2760)
CARD_PITCH = 192
CARD_W = 176

# Bright amber/orange level text on a dark plate.
LV_HSV = ((12, 120, 150), (30, 255, 255))
GLYPH_MIN_AREA = 40
GLYPH_MIN_W = 8
GLYPH_MIN_H = 14
GLYPH_MAX_H = 60
RAIL_MAX_H = 32     # digits 25-26, label 19; the gold scroll icon is 44

DIGIT_SZ = (24, 34)
DIGIT_MAX_D = 0.13
DIGIT_MIN_MARGIN = 1.8

_EX = None


def exemplars():
    global _EX
    if _EX is None:
        _EX = {}
        for p in sorted(glob.glob(os.path.join(DIGIT_DIR, "*.png"))):
            d = os.path.basename(p)[0]
            if not d.isdigit():
                continue
            im = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
            if im is not None:
                _EX.setdefault(d, []).append(_norm(im))
    return _EX


def _norm(m):
    return cv2.resize(m, DIGIT_SZ,
                      interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0


def classify(glyph):
    """(digit, distance, margin) or (None, d, m) when it is not confident."""
    ex = exemplars()
    if not ex:
        return None, 1.0, 0.0
    q = _norm(glyph)
    scored = sorted((float(np.abs(q - e).mean()), d)
                    for d, es in ex.items() for e in es)
    best_d, best = scored[0]
    second = next((d for d, lb in scored if lb != best), None)
    margin = (second / best_d) if best_d > 1e-6 else 1e9
    if best_d > DIGIT_MAX_D or margin < DIGIT_MIN_MARGIN:
        return None, best_d, margin
    return best, best_d, margin


def _glyphs(frame, band=RAIL_BAND, xr=RAIL_X, max_h=RAIL_MAX_H):
    """Bright level glyphs in a band, as (x, y, w, h, mask) left to right.

    `max_h` excludes the card's gold SCROLL ICON, which masks the same amber
    and is 44 px tall against the digits' 25-26. Leaving it in bridged the gap
    between every pair of cards, so the whole rail grouped as one card.
    """
    h, w = frame.shape[:2]
    y0, y1 = max(0, band[0]), min(h, band[1])
    x0, x1 = max(0, xr[0]), min(w, xr[1])
    if y1 - y0 < 8 or x1 - x0 < 8:
        return []
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, np.array(LV_HSV[0], np.uint8),
                    np.array(LV_HSV[1], np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m)
    out = []
    for i in range(1, n):
        x, y, bw, bh, a = st[i]
        if a < GLYPH_MIN_AREA or not (GLYPH_MIN_H <= bh <= max_h):
            continue
        if bw > 60 or bw < GLYPH_MIN_W:
            continue                       # a plate, an icon, or a sliver
        out.append((int(x) + x0, int(y) + y0, int(bw), int(bh),
                    (lab[y:y + bh, x:x + bw] == i).astype(np.uint8) * 255))
    out.sort(key=lambda g: g[0])
    return out


# Within a card the "Lv" label measures 19 and 14 px tall in every card
# observed, and the level digits 25-26. The gap between the two is wide, but
# anything landing in it is REFUSED rather than assigned - a misread level is
# the one failure this module exists to prevent.
# --- friend or NPC ------------------------------------------------------
#
# The friends tab still carries NPC cards at the end of the rail, and NPCs are
# TOKEN-PRICED - the one thing this bot must never spend. So a card has to be
# proven a friend, not merely assumed from which tab is open, and it is proven
# TWICE because either test alone has a failure mode.
#
# 1. COLOUR. A friend's portrait is a pale grey silhouette on a light plate; an
#    NPC card is orange with coloured character art. Measured across the rail:
#
#        friends   S mean  32.8 .. 47.4    frac(S>110) 0.190 .. 0.200
#        NPCs      S mean 103.7 ..117.4    frac(S>110) 0.459 .. 0.720
#
#    A friend who sets a colourful avatar could in principle read high, which
#    is exactly why this is not the only test.
#
# 2. STRUCTURE. A friend card draws "Lv" as two small glyphs (19 and 14 px
#    tall) followed by one or two digits (25-26). Nothing else. The NPC cards
#    break it: one has the label and NO digits, the other has a 37 px wide
#    blob of card art among them. Requiring the exact motif rejects both.
NPC_SAT_FRAC = 0.30          # friends <= 0.20, NPCs >= 0.46
BODY_BOX = (10, 150, 27, 77)  # dx0, dx1, dy0, dy1 from the card's glyph x / band


def _looks_npc(frame, x0, band=RAIL_BAND):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]
    y0 = min(h, band[0] + BODY_BOX[2])
    y1 = min(h, band[0] + BODY_BOX[3])
    xa, xb = min(w, x0 + BODY_BOX[0]), min(w, x0 + BODY_BOX[1])
    if xb - xa < 8 or y1 - y0 < 8:
        return True, 1.0                 # cannot see it: assume NPC, never spend
    body = hsv[y0:y1, xa:xb]
    frac = float((body[:, :, 1] > 110).mean())
    return frac >= NPC_SAT_FRAC, frac


LABEL_MAX_H = 20
DIGIT_MIN_H = 22
CARD_GAP = 60        # glyph gaps: 16-22 within a card, 133 between cards


def cards(frame, band=RAIL_BAND):
    """The recruit rail as a list of dicts, left to right.

    Grouped by GAP rather than by the measured 192 px pitch, so a rail that
    has been paged, or holds a different number of friends, still reads.
    """
    out, group = [], []
    prev = None
    for g in _glyphs(frame, band=band):
        if prev is not None and g[0] - prev > CARD_GAP:
            out.append(group)
            group = []
        group.append(g)
        prev = g[0] + g[2]
    if group:
        out.append(group)

    res = []
    for i, grp in enumerate(out):
        x0 = min(g[0] for g in grp)
        heights = [g[3] for g in grp]
        npc, sat = _looks_npc(frame, x0, band)
        card = {"index": i, "x": x0,
                "centre": (x0 + CARD_W // 2, (band[0] + band[1]) // 2),
                "level": None, "friend": False, "sat": round(sat, 3),
                "heights": heights}

        # THE MOTIF: exactly two label glyphs, then one or two digits.
        # RELATIVE, NOT ABSOLUTE. Exposing the `+` row enlarges the player,
        # which scales every glyph with it - labels 19->21 and digits 25->27
        # were measured - so fixed thresholds put the label in the refusal gap
        # and the whole rail read as "no friends". Splitting against the card's
        # OWN tallest glyph is scale-free and works at either size.
        tall = max(g[3] for g in grp)
        cut = tall * 0.85
        labels = [g for g in grp if g[3] < cut]
        digits = [g for g in grp if g[3] >= cut]
        shaped = (len(labels) == 2 and 1 <= len(digits) <= 2
                  and len(labels) + len(digits) == len(grp)
                  and labels[0][0] < labels[1][0] < digits[0][0])
        if not shaped or npc:
            res.append(card)
            continue

        text = ""
        for g in digits:
            d, _dist, _m = classify(g[4])
            if d is None:
                text = ""
                break
            text += d
        if text:
            card["level"] = int(text)
            card["friend"] = True
        res.append(card)
    return res


# --- the player's own level ----------------------------------------------
# On the team panel the player's plate draws "Lv" ABOVE the number rather
# than beside it, and larger - measured 21x34 against a card's 17x25. One
# exemplar set still serves both, because the comparison normalises: the
# player's "8" at 34 px matched a card-harvested "8" at 25 px with d=0.109 and
# a 2.8x margin.
ME_BAND = (725, 790)
ME_X = (850, 980)
ME_MIN_H = 28
ME_MAX_H = 70

# The `Team n/m` badge in the panel header.
TEAM_BAND = (330, 380)
TEAM_X = (2200, 2330)
TEAM_MIN_H = 12


def player_level(frame, log=None):
    """The player's own level from the team panel, or None if unsure.

    None is a REFUSAL, not a default. Every caller compares recruit levels
    against this, so a wrong value recruits someone above the player - the one
    outcome the rule exists to prevent.
    """
    gs = [g for g in _glyphs(frame, band=ME_BAND, xr=ME_X, max_h=ME_MAX_H)
          if g[3] >= ME_MIN_H]
    if not gs or len(gs) > 3:
        if log:
            log.info("roster: the player's level plate did not read "
                     "(%d glyph(s))", len(gs))
        return None
    text = ""
    for g in gs:
        d, dist, m = classify(g[4])
        if d is None:
            if log:
                log.info("roster: a digit of the player's level is not in the "
                         "exemplar set (d=%.3f, margin=%.1fx) - refusing",
                         dist, m)
            return None
        text += d
    return int(text)


def team_slots(frame):
    """(filled, total) from the `Team n/m` badge, or None.

    The badge is the game's own count, which beats inferring occupancy from
    portraits - it says 0/2 before recruiting and 2/2 after, and it is the
    only signal that survives the panel being redrawn.
    """
    gs = [g for g in _glyphs(frame, band=TEAM_BAND, xr=TEAM_X, max_h=40)
          if g[3] >= TEAM_MIN_H]
    if len(gs) != 2:
        return None
    vals = []
    for g in gs:
        d, _dist, _m = classify(g[4])
        if d is None:
            return None
        vals.append(int(d))
    return vals[0], vals[1]


# --- the recruit buttons -------------------------------------------------
#
# **GREEN IS FREE, BLUE COSTS TOKENS.** Each card carries a round `+` at its
# foot, and the colour says which kind of recruit it is. Measured on one rail:
#
#     GREEN +  x = 941, 1133, 1325, 1517, 1709, 1901   the six friends
#     BLUE  +  x = 2093, 2285                          the two NPC cards
#
# So the operator's rule - never recruit an NPC - reduces to never clicking a
# blue disc, which is a far stronger guarantee than reasoning about which tab
# is open. It joins the HUD's `+` controls on the list of things this bot must
# never press, and it is checked at the moment of clicking rather than
# inferred earlier.
PLUS_GREEN = ((38, 90, 90), (85, 255, 255))
PLUS_BLUE = ((95, 90, 90), (130, 255, 255))
PLUS_W = (55, 130)
PLUS_H = (45, 130)
PLUS_MIN_AREA = 2500

# The `Lv` badges sit a fixed distance above the `+` row - measured, badges at
# y=1370 against buttons at y=1655. Anchoring the rail to the buttons means
# nothing here depends on an absolute y, which matters because reaching the
# buttons at all requires shifting the game (see `reveal_rail`).
RAIL_DY = (-300, -245)
PAIR_DX = 110        # a card and its own button share a column within this
PLUS_OFFSET_X = 79   # measured: the + sits 79 px right of the card's Lv x


def plus_buttons(frame, colour=PLUS_GREEN, below=1200):
    """Round `+` discs of one colour, left to right."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    m = cv2.morphologyEx(
        cv2.inRange(hsv, np.array(colour[0], np.uint8),
                    np.array(colour[1], np.uint8)),
        cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, _lab, st, ce = cv2.connectedComponentsWithStats(m)
    out = []
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] < PLUS_MIN_AREA:
            continue
        w, h = int(st[i, cv2.CC_STAT_WIDTH]), int(st[i, cv2.CC_STAT_HEIGHT])
        if not (PLUS_W[0] <= w <= PLUS_W[1] and PLUS_H[0] <= h <= PLUS_H[1]):
            continue
        if int(st[i, cv2.CC_STAT_TOP]) < below:
            continue
        out.append((int(ce[i][0]), int(ce[i][1])))
    return sorted(out)


def recruitable(frame, me, log=None):
    """[(x, y, level)] - GREEN buttons whose card is a friend at or below `me`.

    Everything is anchored to the `+` row: the rail band is derived from it,
    so this works whether or not the game has been shifted to expose it.
    """
    green = plus_buttons(frame, PLUS_GREEN)
    blue = plus_buttons(frame, PLUS_BLUE)
    if not green:
        if log:
            log.info("roster: no green recruit button is on screen - the rail "
                     "is probably still clipped (see reveal_rail)")
        return []
    row_y = int(round(sum(y for _x, y in green) / len(green)))
    band = (row_y + RAIL_DY[0], row_y + RAIL_DY[1])
    found = cards(frame, band=band)
    out = []
    for gx, gy in green:
        # NEVER a blue one, and never a green one sitting in a blue column.
        if any(abs(gx - bx) < PAIR_DX for bx, _by in blue):
            continue
        near = [c for c in found if abs(c["x"] + 79 - gx) < PAIR_DX]
        if not near:
            continue
        card = near[0]
        if not card["friend"] or card["level"] is None:
            continue
        if me is not None and card["level"] > me:
            continue
        out.append((gx, gy, card["level"]))
    if log:
        log.info("roster: %d green / %d blue button(s); %d recruitable at or "
                 "below Lv%s", len(green), len(blue), len(out), me)
    return out


# --- reaching the buttons at all -----------------------------------------
#
# The `+` row is drawn in the game's bottom 59 CSS px, which focus mode clips:
# the game is 839 CSS tall inside a 780 px wrapper, and `align()` pins it to
# `top:0` so the TOP is visible and the bottom is lost. CLAUDE.md says as much
# - "aligning the top sacrifices the NPC rail at the bottom, which nothing
# here needs" - and recruiting is the thing that needs it.
#
# **GROWING THE FRAME IS NOT THE ANSWER, and that was measured.** Raising the
# wrapper heights does reveal the buttons, by resizing the player with them:
#
#     baseline   container 960x839 | iframe 960x839 | player 960x839
#     revealed   container 960x908 | iframe 960x908 | player 960x909
#
# Resizing `ruffle-player` is this project's oldest hard rule - it desyncs
# click -> stage mapping inside the SWF and the game silently stops
# responding, which reads exactly like a hang. A taller browser VIEWPORT is
# free (the game rect and every anchor were verified identical at 800, 860 and
# 900) but does not help, because the clip is not the viewport. `overflow:
# visible` on the clipping wrapper does not reveal them either.
#
# So the game is SHIFTED instead - position only, which `align()` already does
# and which the site itself does by default (`top:-58.5px`). The cost is the
# top 59 px while shifted, which no part of recruiting looks at.
OVERFLOW_CSS = 59
SHIFT_STYLE_ID = "__nsbotRailShift"

# `align()` writes `#game-container{top:0 !important}`, so an equally specific
# rule would tie and lose on order. `html body #game-container` outranks it.
_SHIFT_ON = ("html body #game-container{top:-%dpx !important;}" % OVERFLOW_CSS)


def reveal_rail(cdp, on=True):
    """Shift the game up by its overflow so the `+` row is on screen.

    Always pair with `on=False`; the caller should do it in a `finally`,
    because leaving the game shifted moves every other absolute geometry in
    the project by 118 captured px.
    """
    js = """
    (() => {
      let s = document.getElementById(%r);
      if (!s) { s = document.createElement('style'); s.id = %r;
                document.head.appendChild(s); }
      s.textContent = %r;
      const g = document.querySelector('#game-container');
      return g ? Math.round(g.getBoundingClientRect().y) : null;
    })()
    """ % (SHIFT_STYLE_ID, SHIFT_STYLE_ID, _SHIFT_ON if on else "")
    y = cdp.call("Runtime.evaluate", expression=js,
                 returnByValue=True)["result"].get("value")
    if not on:
        cdp.call("Runtime.evaluate",
                 expression="typeof window.__nsbotAlign==='function' "
                            "&& window.__nsbotAlign()", returnByValue=True)
    return y


# --- locating the rail at ANY layout -------------------------------------
#
# The `Lv` badge row moves when the game is resized to expose the `+` buttons:
# measured y~1370 at the normal player height and y~1440 once enlarged. A
# fixed offset from the button row therefore cannot serve both, and the first
# version silently found no cards at the enlarged size - which reads as "no
# friends here" rather than as a geometry fault.
#
# So the row is FOUND: it is the densest run of amber glyphs in the lower part
# of the frame, and nothing else on this screen draws a row of them.
RAIL_SEARCH = (1200, 1520)
BADGE_ROW_TOL = 12


def find_rail_band(frame, search=RAIL_SEARCH):
    """The (y0, y1) band holding the `Lv` badge row, or None."""
    gs = _glyphs(frame, band=search, xr=RAIL_X)
    if not gs:
        return None
    rows = {}
    for g in gs:
        key = g[1] // BADGE_ROW_TOL * BADGE_ROW_TOL
        rows.setdefault(key, []).append(g)
    best = max(rows.values(), key=len)
    if len(best) < 4:                    # a rail always shows several cards
        return None
    y0 = min(g[1] for g in best)
    y1 = max(g[1] + g[3] for g in best)
    return (y0 - 6, y1 + 6)


def eligible(frame, me, log=None):
    """[(x, y, level)] for GREEN `+` buttons, STRONGEST FIRST.

    **Strongest first is the point.** The rule is "at or below the player's
    level", and an earlier version satisfied it by taking the WEAKEST two -
    it sorted ascending. The operator wants teammates because some hunts are
    hard to solo, so the correct reading of "at or below" is the highest ones
    that qualify, not merely any.
    """
    green = plus_buttons(frame, PLUS_GREEN)
    blue = plus_buttons(frame, PLUS_BLUE)
    band = find_rail_band(frame)
    if not green or band is None:
        if log:
            log.info("roster: %d green button(s), rail band %s - not ready",
                     len(green), band)
        return []
    found = cards(frame, band=band)
    out = []
    for gx, gy in green:
        if any(abs(gx - bx) < PAIR_DX for bx, _by in blue):
            continue                      # never a token-priced NPC
        near = [c for c in found if abs(c["x"] + PLUS_OFFSET_X - gx) < PAIR_DX]
        if not near or not near[0]["friend"] or near[0]["level"] is None:
            continue
        if me is not None and near[0]["level"] > me:
            continue
        out.append((gx, gy, near[0]["level"]))
    out.sort(key=lambda t: -t[2])         # STRONGEST first
    if log:
        log.info("roster: %d green / %d blue; eligible at or below Lv%s: %s",
                 len(green), len(blue), me, [lv for _x, _y, lv in out])
    return out


# --- doing it ------------------------------------------------------------
#
# REACHING THE BUTTONS COSTS A TEMPORARY RESIZE, and that is a deliberate,
# bounded exception to this project's oldest rule. The `+` row is drawn below
# what Ruffle renders at the normal player height, and everything cheaper was
# measured and failed:
#
#     taller browser viewport   game rect and anchors identical - no help
#     overflow:visible          no reflow - no help
#     shifting the game up      moves it (iframe y 0 -> -59) - buttons still
#                               absent at -59, -90, -120 and -150
#     clicking the 8px sliver   lands, changes nothing
#
# Only enlarging the player draws them (measured 839 -> 909), so the sequence
# grows the page wrappers, clicks, and restores - and the restore is verified,
# not assumed: after one full cycle a control at a known place was clicked and
# the rail responded (mean |diff| 14.64 both ways), so click -> stage mapping
# survives. Keep the window as short as possible and always restore in a
# `finally`.
GROW_CSS = ("html body .site-wrapper,html body #panels-wrapper,"
            "html body .main-content{height:850px !important;}")
GROW_ID = "__nsbotGrowRail"

# The recruit rail's two tabs, bottom-left, at the NORMAL layout.
FRIENDS_TAB = (835, 1268)
NPC_TAB = (955, 1260)


def grow_rail(cdp, on=True):
    js = """
    (() => {
      let s = document.getElementById(%r);
      if (!s) { s = document.createElement('style'); s.id = %r;
                document.head.appendChild(s); }
      s.textContent = %r;
      const f = document.querySelector('#game-container iframe');
      return f ? Math.round(f.getBoundingClientRect().height) : null;
    })()
    """ % (GROW_ID, GROW_ID, GROW_CSS if on else "")
    h = cdp.call("Runtime.evaluate", expression=js,
                 returnByValue=True)["result"].get("value")
    if not on:
        cdp.call("Runtime.evaluate",
                 expression="typeof window.__nsbotAlign==='function' "
                            "&& window.__nsbotAlign()", returnByValue=True)
    return h


PANEL_MIN_CARDS = 4


def panel_open(frame):
    """Is the team panel (with its recruit rail) actually on screen?

    **The caller needs this because a popup swallows the click.** A LOST fight
    raises a promo over the village ("Please use smoke bomb to flee... Go to
    Shop"), which is exactly when the hunt recruits - so Recruit Friends landed
    on the popup and the panel never opened. Without a positive check the
    caller cannot tell that from a rail that is merely empty, and every live
    lap fought solo while a clean manual test passed.

    Keyed on the RAIL ITSELF: a located badge row carrying several cards. Two
    things that were tried and rejected:

        the tab strip     brown plates, and so is village architecture - it
                          fired on the plain village
        the Search field  only drawn on the FRIENDS tab, so it cannot answer
                          the question before the tab is switched

    **This is a CONTEXTUAL check, and the limit is stated rather than papered
    over.** It still answers True on 29 unrelated reference frames - combat
    screens and mission lists, where a row of amber glyphs happens to exist.
    That is acceptable ONLY because of where it is called: immediately after
    pressing Recruit Friends in the village, where the alternatives are the
    panel, the village, or a popup over it. All three of those are answered
    correctly. Do not reuse it as a general "is this the team panel" test
    without tightening it first.
    """
    band = find_rail_band(frame)
    if band is None:
        return False
    return len(cards(frame, band=band)) >= PANEL_MIN_CARDS


def recruit(cap, actor, cdp, log, want=2, settle=3.0):
    """Fill up to `want` party slots from FRIENDS at or below the player.

    Returns the levels recruited. Reads the player's level BEFORE growing the
    rail, because that plate is not where the grown layout puts it.
    """
    # THE PANEL OPENS ON THE NPC TAB. Measured live: with the rail left as
    # found, `plus_buttons` sees zero GREEN discs and eight blue ones, so
    # nothing is recruitable and the lap silently fights solo. The tab is
    # switched BEFORE the layout is grown, because that is the geometry the
    # tab coordinates were measured at.
    # ALWAYS, not "only if it looks like the NPC tab". The conditional version
    # of this never fired: at the un-grown layout the `+` row is below the
    # fold, so `plus_buttons` sees nothing whatever the tab, and the guard read
    # that as "already fine". Clicking the friends tab when it is already
    # selected does nothing, so there is no reason to be clever.
    actor.click_pixel(*FRIENDS_TAB, why="recruit rail: FRIENDS tab")
    time.sleep(2.2)

    me = player_level(cap.frame(gray=False), log)
    if me is None:
        log.info("recruit: the player's level did not read - refusing, since "
                 "every level comparison depends on it")
        return []
    took = []
    try:
        h = grow_rail(cdp, True)
        time.sleep(2.0)
        log.info("recruit: rail exposed (player %s px tall)", h)
        for _ in range(want):
            picks = eligible(cap.frame(gray=False), me, log)
            picks = [p for p in picks if p[2] not in took] or picks
            if not picks:
                log.info("recruit: nobody left at or below Lv%d", me)
                break
            gx, gy, lv = picks[0]         # STRONGEST first
            actor.click_pixel(gx, gy, why=f"recruit Lv{lv} (green +, friend)")
            took.append(lv)
            time.sleep(settle)
    finally:
        grow_rail(cdp, False)
        time.sleep(1.2)
    log.info("recruit: took %s (player Lv%d)", took or "nobody", me)
    return took
