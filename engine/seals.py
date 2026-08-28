#!/usr/bin/env python3
"""Hand-seal minigame (the TP "Potion" family) — read the answer, then repeat it.

WHAT THIS GAME ACTUALLY IS — AND WHY CLAUDE.md WAS WRONG ABOUT IT
-----------------------------------------------------------------
CLAUDE.md recorded this minigame as unsolvable, on two claims that a live
observation disproves:

    "the two slots are card BACKS - they are the empty INPUT, not a revealed
     answer"
    "a 47 fps burst over 5 s across the slot strip caught no reveal"

Both are wrong, and the mistake was WHEN it looked, not how fast. Nothing is
revealed until `Start` is pressed. Press it and the phases are:

    1. READY (~3 s)   the two slot cards FLIP OVER one at a time and show the
                      two hand seals you must reproduce. The ten bottom cards
                      are still face down.
    2. countdown 9..0 the slots STAY revealed; the ten cards flip face up but
                      render GREYED OUT (mean saturation ~30 vs ~162 later).
    3. input          the slots flip back to card backs and the ten cards turn
                      full colour. Now you click two of them, in order.

So it is a memorisation game with a generous look phase, not a 90-way guess
against three lives. The whole earlier conclusion — "it needs a jutsu -> seal
table that is not in the client" — was answering a question the game never asks:
the required seals are shown to you, they just are not shown at the same moment
as the buttons.

MEASURED GEOMETRY (captured px, the pinned 1720x720 viewport)
-------------------------------------------------------------
    Start button        (1735, 398)
    slot cards          (1651, 821) and (1806, 821), about 140x175
    ten seal tiles      x = 1051 + 150*i  for i in 0..9,  y = 1069, about 104x104
    "Skill : N / 4"     tpl/tp_seal_hud.png, 1.000 positive / 0.348 worst negative

Verified by overlaying the grid on a live frame: all ten boxes centre on their
tiles and both slot boxes on their cards.

MATCHING IS ON THE BLUE GLOVE, AND THAT IS NOT AN ARBITRARY CHOICE
------------------------------------------------------------------
The same seal is drawn DIFFERENTLY in the two places: the slot card shows it
small over an animated flame background, the tile shows it filling a brown wooden
frame - and during the only window where both are visible, the tiles are greyed.
So the pixels genuinely do not correspond. Measured separation between the right
tile and the runner-up, over the whole strip:

    metric                     slot A        slot B
    greyscale difference        1.03x         1.09x
    Canny edges                 1.01x         1.03x
    grey+Canny (CardSolver)     1.01x         1.07x
    dark-ink silhouette         1.03x         1.34x
    normalised cross-corr       1.05x         3.49x
    blue glove only            *1.10x        *5.13x

The blue glove is the one element that survives every rendering difference: it
is a saturated blue in both, where the skin tones collide with the flame
background and the ink outline collides with the wooden frame. Masking to it and
tight-cropping to its bounding box normalises position and scale in one step.

CONFIDENCE, AND WHY ABSTAINING IS NOT FREE HERE
-----------------------------------------------
Blue alone is decisive for most seals (5.13x) but not all: two of the ten have
near-identical glove silhouettes and separate by only 1.10x. The decision is
therefore two-stage - blue proposes, and when its margin is thin the dark-ink
metric breaks the tie between blue's top two.

The instinct elsewhere in this project is "when unsure, do not click". **That is
the wrong instinct here, and it was measured.** Once the look phase has passed
the game parks the round waiting for two clicks: the slots are face down, there
is no Start button, and nothing re-triggers a reveal. So abstaining does not
cost "one round" - it strands the round permanently and the mission can never
finish. The only exits are a right answer or a wrong one.

So `play_round(commit=True)`, the default, clicks its best guess and records how
confident it was. `commit=False` is for OBSERVATION ONLY - it abstains, which is
useful for harvesting crops without spending hearts but leaves the round parked.

The decision point that IS free is before pressing Start. Nothing is lost by not
starting a round.

WHAT IS STILL MISSING
---------------------
The matcher is not yet reliable enough to trust on every seal. The honest fix is
a LABELLED CATALOGUE of the ten seals in both renderings - slot art and tile art
- harvested once with `--save-crops` and then matched by identity rather than by
cross-rendering similarity. `ref/auto/tp/seals/` is where those crops land.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

START_XY = (1735, 398)
# The sequence GROWS as the mission progresses - `Skill : 1/4` shows two signs,
# `Skill : 3/4` shows four - so the slot row must be MEASURED, not hardcoded.
# Assuming two cost a round: the bot clicked two signs at 3/4 and could not
# complete a four-sign sequence.
# The row is symmetric about SLOT_CX on the same 150 px pitch as the tiles.
SLOT_CX, SLOT_Y, SLOT_PITCH = 1726, 821, 150
SLOT_HW, SLOT_HH = 70, 85
SLOT_W = 140                           # a slot card's on-screen width
SLOTS = [(1651, 821), (1806, 821)]     # the two-sign default; find_slots overrides
TILES = [(1051 + 150 * i, 1069) for i in range(10)]
TILE_HALF = 52
N_TILES = len(TILES)

# Fraction of blue-glove pixels in a tile once it is live and clickable. Face
# down and greyed both sit at ~0.
TILE_LIVE_BLUE = 0.02
# A revealed slot shows a saturated blue glove; a card back is dark flame art.
SLOT_REVEALED_BLUE = 0.02          # fraction of blue pixels

BLUE_LO, BLUE_HI = (95, 70, 50), (135, 255, 255)

# Accept blue's top pick outright above this margin; below it, ask the ink
# metric to choose between blue's top two.
BLUE_MARGIN = 1.8
INK_MARGIN = 1.10


# The whole panel MOVES. Measured after a page reload: the Start button went
# from y=400 to y=432, which silently misaligned every tile and slot crop and
# collapsed the match margins from 7..14x to 1.0x - the solver picked wrong twice
# in a row on a board it had been reading perfectly.
#
# CLAUDE.md already states the rule for battle geometry - "must be ANCHOR-
# RELATIVE, never absolute" - and it applies here for the same reason. The
# "Skill : N / 4" HUD is the anchor: it is present in EVERY phase (unlike Start)
# and lands at exactly (1106, 255) on every correctly-aligned frame.
# NOTE the anchor template was RE-CUT. The original `tp_seal_hud` included the
# counter digits, so it was cut from a board reading "Skill : 1 / 4" and dropped
# to 0.791 - below its own gate - the moment a mission read "2 / 5". A whole
# mission was reported as "the board is gone" because of it. The template now
# covers only the invariant "Skill :" and scores 1.000 on 1/4, 2/5 and 3/4
# alike, against a 0.356 worst negative. Its match centre moved with the crop.
HUD_REF = (1028, 255)


def anchor_offset(frame, log=None):
    """How far the panel has moved from the reference layout. (0,0) if unknown."""
    from perceive import find
    t = _hud()
    if t is None:
        return (0, 0)
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if t.h > g.shape[0] or t.w > g.shape[1]:
        return (0, 0)
    m, c = find(g, t)
    if not m.found:
        return (0, 0)
    off = (m.center[0] - HUD_REF[0], m.center[1] - HUD_REF[1])
    if log and off != (0, 0):
        log.info("panel offset %s (HUD at %s, conf %.3f)", off, m.center, c)
    return off


def _hud():
    from perceive import Template
    p = os.path.join(ROOT, "tpl", "tp_seal_hud.png")
    return Template("tp_seal_hud", p, threshold=0.88) if os.path.exists(p) else None


def board_present(frame):
    """Is the hand-seal board on screen? Gate every click on this."""
    from perceive import find
    t = _hud()
    if t is None:
        return None
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if t.h > g.shape[0] or t.w > g.shape[1]:
        return None
    return find(g, t)[0].found


def _crop(frame, xy, hw, hh):
    x, y = xy
    return frame[max(0, y - hh):y + hh, max(0, x - hw):x + hw]


def tile_crop(frame, i, off=(0, 0)):
    x, y = TILES[i]
    return _crop(frame, (x + off[0], y + off[1]), TILE_HALF, TILE_HALF)


def find_slots(frame, lo=1300, hi=2200, step=12, dark=230.0, off=(0, 0)):
    """Where the slot cards are, and how many. Measured per frame.

    The parchment behind the row reads a flat 255; a card is always darker. The
    gate has to be generous, though: the two cards are not drawn alike - measured
    on one frame the left card sat at 190..215 while the right was 81..132 - so a
    tight threshold finds only the dark one and reports a single slot.
    """
    xs = []
    yy = SLOT_Y + off[1]
    for x in range(lo + off[0], hi + off[0], step):
        c = frame[yy - 60:yy + 60, max(0, x - 6):x + 6]
        if c.size == 0:
            continue
        v = float(cv2.cvtColor(c, cv2.COLOR_BGR2HSV)[:, :, 2].mean())
        if v < dark:
            xs.append(x)
    if not xs:
        return [(x + off[0], y + off[1]) for x, y in SLOTS]
    x0, x1 = min(xs), max(xs)
    # The band runs from the LEFT edge of the first card to the RIGHT edge of the
    # last, so its width is (n-1)*pitch + card_width, not (n-1)*pitch. Forgetting
    # the card width over-counts by one every time.
    span = (x1 - x0) - SLOT_W
    n = max(1, int(round(span / float(SLOT_PITCH))) + 1)
    n = min(n, 8)
    cand = [(int(SLOT_CX + SLOT_PITCH * (i - (n - 1) / 2.0)) + off[0],
             SLOT_Y + off[1]) for i in range(n)]
    # THEN KEEP ONLY THE POSITIONS THAT ACTUALLY HOLD A CARD. The band width is
    # a good first estimate and a bad final answer: measured live, a five-slot
    # round produced a dark run 896 px wide that implied six, and the sixth
    # position was bare parchment. The bot then waited forever for a sixth sign
    # that was never coming - "recorded 5 of 6 sign(s)" on a loop.
    # A card is dark; the parchment behind the row is ~255.
    keep = []
    for (x, y) in cand:
        c = frame[max(0, y - 50):y + 50, max(0, x - 45):x + 45]
        if c.size == 0:
            continue
        if float(cv2.cvtColor(c, cv2.COLOR_BGR2HSV)[:, :, 2].mean()) < 215.0:
            keep.append((x, y))
    return keep or cand


def slot_crop(frame, i, slots=None, off=(0, 0)):
    ss = slots if slots is not None else SLOTS
    x, y = ss[i]
    return _crop(frame, (x + off[0], y + off[1]), SLOT_HW, SLOT_HH)


def blue_mask(c):
    if c is None or c.size == 0:
        return None
    return cv2.inRange(cv2.cvtColor(c, cv2.COLOR_BGR2HSV), BLUE_LO, BLUE_HI)


def ink_mask(c):
    if c is None or c.size == 0:
        return None
    return cv2.inRange(cv2.cvtColor(c, cv2.COLOR_BGR2GRAY), 0, 80)


def _shape(mask, sz=64, min_px=40):
    """Tight-crop a mask to its content and normalise to a fixed square.

    Tight-cropping is what makes a slot and a tile comparable at all: the same
    seal is drawn at different sizes and offsets in the two places, and the
    bounding box of the glove removes both differences.
    """
    if mask is None:
        return None
    ys, xs = np.nonzero(mask)
    if len(xs) < min_px:
        return None
    m = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    return cv2.resize(m, (sz, sz), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0


def dist(a, b):
    if a is None or b is None:
        return 1e9
    return float(np.abs(a - b).mean())


def tiles_live(frame, off=(0, 0)):
    """Are the ten tiles showing full-colour, clickable seals?

    KEYED ON THE BLUE GLOVE, NOT ON SATURATION. Saturation was the obvious
    choice and it is wrong twice over: a FACE-DOWN card is orange flame art and
    reads as saturated as a live seal, so the check fired on a board that had not
    dealt yet. It cost two rounds - once by never pressing Start, once by trying
    to read seals off card backs.

    Blue separates all three states cleanly, because only a live seal has a
    saturated blue glove:

        face down (flame back)   ~0.00 blue
        greyed during countdown  ~0.00 blue   (the art is desaturated)
        live, clickable          >0.02 blue
    """
    fr = []
    for i in range(N_TILES):
        c = tile_crop(frame, i, off)
        if c is None or c.size == 0:
            return False, []
        m = blue_mask(c)
        fr.append(0.0 if m is None else float((m > 0).mean()))
    return (sum(v >= TILE_LIVE_BLUE for v in fr) >= N_TILES - 1), fr


def slots_revealed(frame, slots=None, off=(0, 0)):
    """Are ALL the slot cards showing a sign rather than a card back?"""
    ss = slots if slots is not None else find_slots(frame, off=off)
    out = []
    for i in range(len(ss)):
        m = blue_mask(slot_crop(frame, i, ss))
        out.append(0.0 if m is None else float((m > 0).mean()))
    return (bool(out) and all(v >= SLOT_REVEALED_BLUE for v in out)), out


def same_seal(a, b, gate=0.12):
    """Are two SLOT-rendered crops the same seal?

    Both sides are drawn identically here - same size, same card, same flame
    background - so this is a like-for-like comparison, and it is the reliable
    one. It exists because the cross-rendering slot-to-tile match is not:
    measured 5.13x separation on some seals and only 1.10x on others.
    """
    ma, mb = _shape(blue_mask(a)), _shape(blue_mask(b))
    if ma is None or mb is None:
        return False, 1e9
    d = dist(ma, mb)
    return d <= gate, d


def rank_candidates_from(art, tile_frame, log=None, off=(0, 0)):
    """For each recorded sign, the tiles ranked best-first. None if unreadable."""
    tiles = [_shape(blue_mask(tile_crop(tile_frame, i, off))) for i in range(N_TILES)]
    if sum(t is not None for t in tiles) < N_TILES:
        if log:
            log.info("only %d/%d tiles readable",
                     sum(t is not None for t in tiles), N_TILES)
        return None
    out = []
    for s, a in enumerate(art):
        sb = _shape(blue_mask(a)) if a is not None else None
        if sb is None:
            if log:
                log.info("sign %d was never captured", s)
            return None
        out.append(sorted((dist(sb, t), i) for i, t in enumerate(tiles)))
    return out


def rank_candidates(reveal_frame, tile_frame, log=None):
    """Snapshot variant, kept for offline analysis of a single frame."""
    ss = find_slots(reveal_frame)
    return rank_candidates_from([slot_crop(reveal_frame, i, ss)
                                 for i in range(len(ss))], tile_frame, log)


def read_answer(reveal_frame, tile_frame, log=None, force=False):
    """Which two tiles are the answer? Returns [i, j] or None if unsure.

    `reveal_frame` is from the look phase (slots showing the seals);
    `tile_frame` is any frame where the tiles are drawn in colour. They are
    deliberately allowed to be different frames, because the game never shows
    both at once.
    """
    tiles = [_shape(blue_mask(tile_crop(tile_frame, i))) for i in range(N_TILES)]
    tiles_ink = [_shape(ink_mask(tile_crop(tile_frame, i))) for i in range(N_TILES)]
    if sum(t is not None for t in tiles) < N_TILES:
        if log:
            log.info("only %d/%d tiles readable - not guessing",
                     sum(t is not None for t in tiles), N_TILES)
        return None

    picks = []
    for s in range(len(SLOTS)):
        sb = _shape(blue_mask(slot_crop(reveal_frame, s)))
        if sb is None:
            if log:
                log.info("slot %d is not showing a seal - nothing to read", s)
            return None
        ranked = sorted((dist(sb, t), i) for i, t in enumerate(tiles))
        (d0, i0), (d1, i1) = ranked[0], ranked[1]
        margin = d1 / max(1e-6, d0)
        if margin >= BLUE_MARGIN:
            picks.append(i0)
            if log:
                log.info("slot %d -> tile %d (blue d=%.3f, margin %.2fx)",
                         s, i0, d0, margin)
            continue
        # Thin margin: let the ink outline choose between the top two.
        si = _shape(ink_mask(slot_crop(reveal_frame, s)))
        e0, e1 = dist(si, tiles_ink[i0]), dist(si, tiles_ink[i1])
        lo, hi = (i0, e0), (i1, e1)
        if e1 < e0:
            lo, hi = (i1, e1), (i0, e0)
        if hi[1] / max(1e-6, lo[1]) < INK_MARGIN and not force:
            if log:
                log.info("slot %d is AMBIGUOUS: blue says %d/%d at %.2fx and the "
                         "ink tie-break is %.2fx - abstaining rather than "
                         "spending a heart", s, i0, i1, margin,
                         hi[1] / max(1e-6, lo[1]))
            return None
        picks.append(lo[0])
        if log:
            log.info("slot %d -> tile %d (blue %.2fx was thin; ink tie-break "
                     "%.2fx)", s, lo[0], margin, hi[1] / max(1e-6, lo[1]))
    return picks


def capture_sequence(cap, log=None, timeout=25.0, poll=0.10):
    """Watch the whole reveal and record each sign AS IT APPEARS.

    THE SIGNS ARE SHOWN ONE AT A TIME, not all at once. Measured live on a
    four-sign round: mid-reveal the slot blue fractions read
    [0.262, 0.182, 0.000, 0.000] - two signs up, two still to come.

    A single snapshot therefore cannot read the sequence, and a gate that
    requires every slot to be filled simultaneously never fires at all: by the
    time the last sign appears the first ones may already be flipping back. That
    is why an earlier version sat through the entire look phase reporting "the
    slots never revealed a sign".

    So this polls and keeps the FIRST frame in which each slot shows a sign,
    which also preserves the ORDER - the thing the game actually tests.

    Returns (art, slots): art[i] is slot i's crop, or None if it never showed.
    """
    t0 = time.time()
    slots = None
    art = None
    seen = 0
    off = (0, 0)
    while time.time() - t0 < timeout:
        f = cap.frame(gray=False)
        if slots is None:
            off = anchor_offset(f, log)
            slots = find_slots(f, off=off)
            art = [None] * len(slots)
        for i in range(len(slots)):
            if art[i] is not None:
                continue
            m = blue_mask(slot_crop(f, i, slots))
            if m is not None and float((m > 0).mean()) >= SLOT_REVEALED_BLUE:
                art[i] = slot_crop(f, i, slots).copy()
                seen += 1
                if log:
                    log.info("   sign %d of %d shown", i + 1, len(slots))
        if seen == len(slots):
            break
        time.sleep(poll)
    return art, (slots or list(SLOTS)), off


def wait_for(cap, pred, timeout, poll=0.12):
    """Poll until `pred(frame)` is true. Returns the frame, or None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        f = cap.frame(gray=False)
        if pred(f):
            return f
        time.sleep(poll)
    return None


def play_round(cap, actor, log, save_crops=False, commit=True,
               recover_parked=True):
    """One round: Start -> read the answer -> click the two tiles. True if played.

    With `commit` (the default) a low-confidence read is still played, because
    once the look phase is over the round is parked until two tiles are clicked -
    see the module docstring. `commit=False` observes without spending hearts and
    leaves the round stranded on purpose.
    """
    # Park the scroll first. The game is 839 CSS px tall in a 720 px viewport,
    # so part of it is always off screen; if the tile strip is the part that is
    # cut off, no amount of offset correction helps - the pixels are not there.
    # Measured: a run reported "the tiles never became active" on a board whose
    # tiles were perfectly active, just scrolled out of view.
    try:
        cap.scroll_game(0.0)
        time.sleep(0.3)
    except Exception:
        pass

    f = cap.frame(gray=False)
    if board_present(f) is False:
        log.info("hand-seal board is not on screen - refusing to click a fixed "
                 "grid at nothing")
        return False

    # Start is only present between rounds. Detect it BY TEMPLATE - the first
    # version inferred it from tile saturation and got it exactly backwards,
    # because a face-down card is orange flame art and reads as saturated as a
    # live seal. It concluded a round was already running, never pressed Start,
    # and sat out the whole look phase.
    # tp_seal_start: 1.000 positive, 0.248 worst negative.
    from perceive import Template, find as _find
    sp = os.path.join(ROOT, "tpl", "tp_seal_start.png")
    if os.path.exists(sp):
        m, sc = _find(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY),
                      Template("tp_seal_start", sp, threshold=0.88))
        if m.found:
            log.info("pressing Start (%.3f)", sc)
            actor.click_pixel(*m.center, why="Start the hand-seal round")
        else:
            log.info("no Start button - a round is already in progress")
    else:
        actor.click_pixel(*START_XY, why="Start the hand-seal round")

    art, slots, off = capture_sequence(cap, log, timeout=25.0)
    got = sum(a is not None for a in art)
    if got:
        log.info("look phase: recorded %d of %d sign(s), in order", got, len(art))
    reveal = None if got < len(art) else True
    if reveal is None:
        # A PARKED ROUND. The look phase happened while nobody was watching -
        # the previous attempt stopped, or the countdown expired - so the signs
        # are hidden, there is no Start button, and the game will wait forever
        # for clicks we cannot possibly get right.
        #
        # The ONLY exit is to click. A miss costs one heart but rerolls the
        # round, which brings back the Start button and a look phase we can
        # actually read; leaving it parked forfeits the whole mission. So this
        # spends a heart deliberately, and says so.
        f2 = cap.frame(gray=False)
        if recover_parked and tiles_live(f2)[0] and board_present(f2):
            slots = find_slots(f2)
            log.info("round is PARKED (%d signs, none visible, no Start). "
                     "Spending one heart to reroll it - the alternative is "
                     "forfeiting the mission.", len(slots))
            for k in range(len(slots)):
                g = cap.frame(gray=False)
                if board_present(g) is False:
                    return False
                o2 = anchor_offset(g)
                actor.click_pixel(TILES[k % N_TILES][0] + o2[0],
                                  TILES[k % N_TILES][1] + o2[1],
                                  why=f"clear parked round ({k + 1}/{len(slots)})")
                time.sleep(0.4)
            return False
        log.info("the slots never revealed a sign - not guessing")
        return False
    log.info("look phase: both slots are showing their seal")

    # The tiles are greyed during the look phase, so wait for them to go live and
    # read their artwork from THAT frame. Positions do not move in between.
    tf = wait_for(cap, lambda x: tiles_live(x, off)[0], timeout=20.0)
    if tf is None:
        log.info("the tiles never became active")
        return False

    if save_crops:
        d = os.path.join(ROOT, "ref/auto/tp/seals")
        os.makedirs(d, exist_ok=True)
        ts = int(time.time() * 1000)
        for i, a in enumerate(art):
            if a is not None:
                cv2.imwrite(os.path.join(d, f"{ts}_sign{i}.png"), a)
        for i in range(N_TILES):
            cv2.imwrite(os.path.join(d, f"{ts}_tile{i}.png"), tile_crop(tf, i, off))

    # `art` already holds the signs AS SHOWN, in order - captured one at a time
    # as they appeared, which is the order the game tests.
    want = art

    ranked = rank_candidates_from(art, tf, log, off)
    if ranked is None:
        log.info("cannot read the signs at all - nothing to click")
        return False
    if not commit:
        log.info("commit=False - not clicking (this leaves the round parked)")
        return False

    picked, ok_all = [], True
    for si in range(len(want)):
        by_i = {i: d for d, i in ranked[si]}
        order = [i for _, i in ranked[si] if i not in picked]
        if not order:
            return False
        choice = order[0]
        margin = (by_i[order[1]] / max(1e-6, by_i[choice])) if len(order) > 1 else 99.0

        # THIN MARGIN: ask a second, independent metric. Two of the ten seals
        # have near-identical glove silhouettes and separate by only ~1.1x on
        # blue alone - and every wrong pick this session has come from exactly
        # that band, while every pick above ~2.4x has been right. The dark-ink
        # outline is a different feature of the same art, so where blue cannot
        # choose, ink usually can.
        if margin < BLUE_MARGIN and len(order) > 1 and want[si] is not None:
            a, b = order[0], order[1]
            si_ink = _shape(ink_mask(want[si]))
            ea = dist(si_ink, _shape(ink_mask(tile_crop(tf, a, off))))
            eb = dist(si_ink, _shape(ink_mask(tile_crop(tf, b, off))))
            if eb < ea:
                log.info("   blue was thin (%.2fx); ink prefers tile %d over %d "
                         "(%.3f vs %.3f) - switching", margin, b, a, eb, ea)
                choice = b
            else:
                log.info("   blue was thin (%.2fx); ink agrees on tile %d "
                         "(%.3f vs %.3f)", margin, a, ea, eb)
        log.info("sign %d -> tile %d (d=%.3f, margin %.2fx)",
                 si, choice, by_i[choice], margin)

        # RE-DERIVE THE OFFSET IMMEDIATELY BEFORE CLICKING, from a fresh frame.
        # The panel moves between the decision and the action - the page scroll
        # drifted 458 -> 301 -> 242 across one session, taking the panel with it -
        # so an offset measured once per round is stale by the time the click
        # goes out, and the click lands on the wrong tile or on nothing.
        # Deciding and acting are separate moments; only the acting one counts.
        g = cap.frame(gray=False)
        if board_present(g) is False:
            log.info("board vanished mid-answer - stopping")
            return False
        now = anchor_offset(g)
        if now != off:
            log.info("   panel moved %s -> %s since the read; clicking at the "
                     "current position", off, now)
        actor.click_pixel(TILES[choice][0] + now[0], TILES[choice][1] + now[1],
                          why=f"hand sign {si} = tile {choice}")
        time.sleep(0.6)

        # VERIFY IN THE SAME RENDERING. Clicking a tile fills the slot with that
        # seal drawn as SLOT art - exactly the rendering we memorised. Comparing
        # those two is like-for-like, not the cross-rendering guess that makes
        # the tile lookup unreliable in the first place. It says whether the pick
        # was right, and yields a labelled example for free.
        after = cap.frame(gray=False)
        got = slot_crop(after, si, slots)
        verdict, dd = same_seal(want[si], got)
        log.info("   filled slot %d %s (d=%.3f)", si,
                 "MATCHES the sign shown" if verdict else "does NOT match",
                 dd)
        if not verdict:
            ok_all = False
        if save_crops:
            d2 = os.path.join(ROOT, "ref/auto/tp/seals")
            os.makedirs(d2, exist_ok=True)
            ts = int(time.time() * 1000)
            cv2.imwrite(os.path.join(d2, f"{ts}_s{si}_shown.png"), want[si])
            cv2.imwrite(os.path.join(d2, f"{ts}_s{si}_filled_tile{choice}_"
                                         f"{'ok' if verdict else 'bad'}.png"), got)
            cv2.imwrite(os.path.join(d2, f"{ts}_s{si}_tile{choice}_art.png"),
                        tile_crop(tf, choice, off))
        picked.append(choice)

    log.info("played %s - %s", picked,
             "both signs verified" if ok_all else "at least one sign was wrong")
    return ok_all


def play(cap, actor, log, max_rounds=12, save_crops=False):
    """Play the WHOLE mission - every round until the board is gone.

    A round is not a mission. `Skill : N / 4` (sometimes N / 5) means the board
    has to be beaten several times, and the sequence grows as it goes. Playing a
    single round and then trying to close out is how a run ended with "close-out
    timed out after 45s" on a mission that was still very much in progress.

    Stops when the board disappears (mission over, won or lost) or when a round
    cannot be played at all.
    """
    won = 0
    for r in range(max_rounds):
        f = cap.frame(gray=False)
        if board_present(f) is not True:
            log.info("the hand-seal board is gone after %d round(s)", r)
            break
        log.info("--- hand-seal round %d ---", r + 1)
        ok = play_round(cap, actor, log, save_crops=save_crops)
        if ok:
            won += 1
        elif ok is False and board_present(cap.frame(gray=False)) is not True:
            break
        time.sleep(2.5)
    log.info("hand-seal mission: %d round(s) played cleanly", won)
    return won


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
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--save-crops", action="store_true")
    ap.add_argument("--probe", action="store_true",
                    help="report the phase and the geometry WITHOUT clicking")
    a = ap.parse_args()
    log = _Log()

    t = find_page_target(port=a.port, url_contains="ninjasaga", timeout=20)
    c = CDP(t["webSocketDebuggerUrl"])
    c.call("Page.enable")
    cap = Capture(c)
    try:
        if a.probe:
            f = cap.frame(gray=False)
            live, sats = tiles_live(f)
            rev, blues = slots_revealed(f)
            print(f"  board       {board_present(f)}")
            print(f"  tiles live  {live}  (mean sat {np.mean(sats):.1f})")
            print(f"  slots shown {rev}  (blue fraction {[round(b,3) for b in blues]})")
            return 0
        actor = Actor(c, cap, log, dry_run=False,
                      click_delay=(0.03, 0.08), post_click=(0.03, 0.08))
        for r in range(a.rounds):
            log.info("--- round %d ---", r + 1)
            if not play_round(cap, actor, log, save_crops=a.save_crops):
                break
            time.sleep(2.0)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
