"""Combat decision layer — encodes what two full missions actually taught us.

Every guard in here exists because of an observed failure, not a hypothetical.
"""
import time

import cv2
import numpy as np

from perceive import find, mask_stats, HP_FILL_BGR


# ---------------------------------------------------------------------------
# Turn gating
# ---------------------------------------------------------------------------
def is_my_turn(frame_gray, attack_tpl):
    """The command bar's PRESENCE is the turn gate.

    Measured alternatives that do NOT work:
      * turn-marker position: the Victory panel draws over the bar, and the
        marker sits at ~99% both when it is your turn and when it is not.
      * fixed timing: clicks issued during the enemy phase are silently
        discarded. Roughly a third of a mission's worth of actions were lost
        this way before switching to detection.
    """
    m, _ = find(frame_gray, attack_tpl)
    return m.found


# ---------------------------------------------------------------------------
# Cooldown detection
# ---------------------------------------------------------------------------
class SlotBaseline:
    """Per-slot cooldown detection by comparison against a known-ready sample.

    A GLOBAL saturation threshold does not work. Measured across the 8 slots in
    one live frame, mean saturation ran 56.2 .. 190.8 CONTINUOUSLY, with no
    bimodal split: the pale-pink slot reads 56 while fully usable, purely because
    of its palette. A fixed cutoff would mark it permanently on cooldown.

    So each slot is compared against its own baseline, captured once while ready.
    """

    def __init__(self, drop_ratio=0.55):
        self.baseline = {}
        self.drop_ratio = drop_ratio

    @staticmethod
    def _sat(frame_bgr, cx, cy, r=20):
        patch = frame_bgr[cy - r:cy + r, cx - r:cx + r]
        if patch.size == 0:
            return None
        return float(cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)[:, :, 1].mean())

    def learn(self, frame_bgr, slots):
        """Record each slot's ready-state saturation. Call when all are ready."""
        for name, (cx, cy) in slots.items():
            s = self._sat(frame_bgr, cx, cy)
            if s is not None:
                self.baseline[name] = s
        return dict(self.baseline)

    def on_cooldown(self, frame_bgr, name, cx, cy):
        base = self.baseline.get(name)
        s = self._sat(frame_bgr, cx, cy)
        if base is None or s is None:
            return None                      # unknown, not False - do not guess
        return s < base * self.drop_ratio, s, base


# ---------------------------------------------------------------------------
# Progress watchdog  ** the most important guard here **
# ---------------------------------------------------------------------------
class DamageWatchdog:
    """Abort a fight that is not being won.

    Observed live: enemy HP went 50.7 -> 43.0 -> 43.0 -> 43.0 -> 47.2 percent.
    It went back UP. Enemies regenerate, exactly as the player does (+250 HP
    ticks are visible mid-combat). A basic-attack loop doing ~8pp per hit can be
    fully cancelled by that regen, producing an unwinnable fight that shows no
    error and never ends.

    Without this guard a bot would grind such a fight until the 50-round cap or
    forever. With it, the bot gives up and takes `Run`.
    """

    def __init__(self, stall_turns=3, regen_tolerance_pp=1.0):
        self.stall_turns = stall_turns
        self.regen_tolerance_pp = regen_tolerance_pp
        self.history = []
        self.counts = []

    def observe(self, enemy_fill_pct, enemy_count=None):
        """Record a turn. `enemy_count` makes KILLING one count as progress.

        THE SCALAR MUST NOT GO UP WHEN YOU ARE WINNING. Fed the LOWEST enemy
        bar, this watchdog fires when the weakest enemy DIES: the minimum over
        the survivors jumps up, which is indistinguishable from regeneration.
        Measured in a live fight, the reading went 18.4 -> 39.7 the moment the
        low-HP enemy dropped off the list, and the watchdog called it
        "regenerating" and fled a mission that was being won.

        So the caller now feeds TOTAL enemy HP, which falls both when an enemy
        is damaged and when one dies, and the count is tracked alongside: a
        fight with fewer enemies than before is making progress whatever the
        percentages say.
        """
        self.history.append(float(enemy_fill_pct))
        self.counts.append(None if enemy_count is None else int(enemy_count))
        return self.verdict()

    def verdict(self):
        """Count turns since a NEW LOW was achieved.

        An earlier version compared window-start to window-end, which a single
        early good hit masks completely: on the real sequence
        50.7 -> 43.0 -> 43.0 -> 43.0 -> 47.2 it reported "continue", because
        50.7-47.2 = 3.5pp still looked like progress. Tracking the best value
        actually reached is what catches a fight going nowhere.
        """
        h = self.history
        if len(h) < 2:
            return "continue"
        best = min(h)
        # Turns elapsed since we FIRST reached the best value. Using the last
        # occurrence (or breaking on equality) undercounts: three consecutive
        # turns parked at 43.0% are three turns of no progress, not one.
        first_best = min(i for i, v in enumerate(h) if v <= best + 1e-9)
        since_best = (len(h) - 1) - first_best
        # Killing an enemy IS progress, even if no new low in total HP followed.
        c = [x for x in self.counts if x is not None]
        if len(c) >= 2 and len(c) == len(h):
            fewest = min(c)
            first_fewest = min(i for i, v in enumerate(c) if v <= fewest)
            since_best = min(since_best, (len(c) - 1) - first_fewest)
        if since_best >= self.stall_turns:
            if h[-1] > best + self.regen_tolerance_pp:
                return "regenerating"
            return "stalled"
        return "continue"


def enemy_bar_fill(frame_bgr, x, y, w, h=10):
    """Enemy HP as a percentage. ALWAYS measure - never judge a bar by eye.

    A bar visually read as "a sliver" measured 43-56%. Four separate wrong
    conclusions this session came from eyeballing bars; the calibrated mask got
    it right every time.
    """
    lo, hi = HP_FILL_BGR
    m = cv2.inRange(frame_bgr[y:y + h, x:x + w],
                    np.array(lo, np.uint8), np.array(hi, np.uint8))
    cols = np.nonzero(m.sum(axis=0))[0]
    return (cols.max() + 1) / w * 100 if len(cols) else 0.0


# The PLAYER HUD lives in the top band of the frame and is built from the same
# bright red as an enemy HP bar, so a scan that starts at y=0 returns it as
# enemies. Measured on a live 1440-tall combat frame:
#
#     player HUD   y =  40,  60,  87, 102   ( 2.8% .. 7.1% down)  all 51.0%
#     real enemies y = 620, 835, 881        (43.1% .. 61.2% down)  25.9 .. 30.9%
#
# The HUD entries are identical every turn while real bars move, which is how
# they were spotted: a fight showed `enemies=10, total=418.1%` when three
# enemies were on screen. That inflates the watchdog's total and count, and a
# bar-derived click in that row would land in the HUD - which is where the token
# `+` sinks are. The gap between 7.1% and 43.1% is wide, so a 15% floor clears
# the HUD with room to spare and still sits far above any real bar seen.
HUD_GUARD_FRAC = 0.15


def find_enemy_bars(frame_bgr, x0, x1, y0, y1, bar_h=10, min_run=40,
                    hud_guard=HUD_GUARD_FRAC):
    """Locate every enemy HP bar by scanning VERTICALLY.

    Multi-enemy encounters are the norm (2, 3 and 4 seen). Each enemy's plate and
    bar sit at their own y, so a single fixed bar position is wrong. Returns
    [(y, fill_pct)] top to bottom.

    `hud_guard` floors the scan below the player HUD; pass 0 to disable it.
    """
    lo, hi = HP_FILL_BGR
    if hud_guard:
        y0 = max(y0, int(frame_bgr.shape[0] * hud_guard))
    if y1 <= y0:
        return []
    region = frame_bgr[y0:y1, x0:x1]
    m = cv2.inRange(region, np.array(lo, np.uint8), np.array(hi, np.uint8))
    rows = (m > 0).sum(axis=1)
    out, y = [], 0
    while y < len(rows):
        if rows[y] >= min_run:
            band = rows[y:y + bar_h]
            yy = y + int(np.argmax(band))
            out.append((y0 + yy, enemy_bar_fill(frame_bgr, x0, y0 + yy, x1 - x0)))
            y += bar_h * 2
        else:
            y += 1
    return out


# ---------------------------------------------------------------------------
# READING COOLDOWN OFF THE ICON - which this file long said was impossible.
#
# **CORRECTION, and the old measurement was aimed at the wrong pixels rather
# than simply wrong.** The standing rule was "do not use icon saturation as the
# primary cooldown signal", on the evidence that mean saturation across the
# eight slots ran 56.2 .. 190.8 CONTINUOUSLY with no bimodal split, and that a
# pale-pink slot read 56 while perfectly usable.
#
# That mean was taken over the WHOLE TILE - including the metal border, which
# stays coloured whatever the state and drags a cooling tile up into the usable
# range. Measured on the tile INTERIOR instead, on live frames where three
# skills were cooling and one was ready:
#
#     slot   mean sat   frac(sat > 60)   state
#     S1         0.0         0.000       cooling
#     S2       164.0         0.905       READY
#     S3         0.0         0.000       cooling
#     S4         0.0         0.000       cooling
#
# A cooling skill is drawn in TRUE GREYSCALE - saturation identically zero, not
# merely low - which is about as clean a signal as this project has found. The
# pale-pink counter-example was a COLOURED tile with washed-out art: it has
# some saturation, whereas a cooling tile has none at all.
#
# The game also prints the remaining count on the tile (7, 22 and 10 were
# observed on the three cooling slots), so an exact countdown is there if it is
# ever wanted. The bot does not need it: "may I use this now" is the only
# question being asked, and greyscale answers it with no digit reading.
#
# SAMPLE CAVEAT, stated plainly: this comes from two frames six seconds apart
# in ONE battle - one ready example, three cooling. The separation is total and
# the mechanism is a standard convention, but a second ready slot on a
# different frame would make it solid. The gate sits well below the observed
# ready value so a washed-out-but-ready tile has room.
COOLING_SAT = 60          # a pixel this saturated counts as coloured
COOLING_FRAC = 0.25       # ready measured 0.905, cooling 0.000
# The interior, as an inset from the tile centre - deliberately inside the
# border, which is exactly what poisoned the original measurement.
COOLING_BOX = (38, 42, 30)   # half-width, up from centre, down from centre


def slot_cooling(frame_bgr, centre):
    """Is the skill at `centre` greyed out (cooling)? True / False / None.

    None means the tile could not be sampled - off-frame or degenerate - and a
    caller must treat that as UNKNOWN, never as ready or cooling.
    """
    if frame_bgr is None or centre is None:
        return None
    dx, up, down = COOLING_BOX
    cx, cy = int(centre[0]), int(centre[1])
    h, w = frame_bgr.shape[:2]
    x0, x1 = max(0, cx - dx), min(w, cx + dx)
    y0, y1 = max(0, cy - up), min(h, cy + down)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    box = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    frac = float((box[:, :, 1] > COOLING_SAT).mean())
    return frac < COOLING_FRAC


# ---------------------------------------------------------------------------
# Cooldowns - ROUND bookkeeping, as a cross-check and a fallback
# ---------------------------------------------------------------------------
class CooldownTracker:
    """Track skill cooldowns by counting ROUNDS, not by reading icons.

    The game's own battle processor decrements cooldowns once per round
    (`nextRound()` -> `reduceSkillCooldown(1)`), so cooldowns are an integer
    number of rounds. That makes them exactly trackable by bookkeeping: record
    the round a slot was used, and it is ready again `cd_rounds` later.

    This replaces saturation-based detection, which was measured to be unusable
    as a global threshold (slot saturation ran 56..191 continuously, with a pale
    slot reading 56 while fully ready). Keep `SlotBaseline` only as an optional
    cross-check, never as the primary signal.
    """

    def __init__(self, cooldowns=None):
        self.round = 0
        self.cooldowns = dict(cooldowns or {})   # slot -> cd length in rounds
        self.used_at = {}                        # slot -> round it was used
        self.learned = {}                        # slot -> length measured in play
        self._lo = {}                            # slot -> cooldown is >= this
        self._hi = {}                            # slot -> cooldown is <= this

    def next_round(self):
        self.round += 1
        return self.round

    def use(self, slot):
        self.used_at[slot] = self.round

    def ready(self, slot):
        cd = self.cooldowns.get(slot)
        if cd is None:
            cd = self.learned.get(slot)          # measured in play, if we have it
        if cd is None:
            return None                          # unknown length - do not guess
        used = self.used_at.get(slot)
        return True if used is None else (self.round - used) >= cd

    # ------------------------------------------------------------------
    # LEARNING THE LENGTHS, from outcomes we already record.
    #
    # The lengths are not in the game's client - `SKILL_DATA` is populated at
    # runtime by the server - so this file's own note says each slot has to be
    # measured in game and written into the config by hand. That never happened
    # for a single slot (`rounds_per_slot` is still `{}`), so `ready()` has
    # always returned None and the rotation has always fallen back to
    # rotate-on-resolve. Which means the bot re-clicks a cooling skill, waits
    # out the full ~6 s resolve timeout, and only then tries something else.
    #
    # But the length can be BRACKETED from what already happens, with no new
    # perception whatsoever:
    #
    #     a slot USED at round U and used again SUCCESSFULLY at round R
    #         -> the cooldown is at most R - U
    #     a slot USED at round U that FAILED at round R
    #         -> the cooldown is more than R - U
    #
    # Squeeze the two together and the value is exact. It is the same shape as
    # the Mastermind solver in `kekkai.py`: keep every candidate consistent
    # with the evidence and wait for one to survive.
    #
    # THE FAILURE SIDE IS THE DANGEROUS HALF, because a skill can fail for
    # reasons that have nothing to do with cooldown - a stun, too little CP, a
    # click that missed. A lower bound learned from one of those would be a
    # lie, and a lie here DISABLES A WORKING SKILL. So a failure is only
    # admitted as evidence when the caller can say the turn was otherwise
    # healthy (see `observe_failure`), and a length is only TRUSTED once the
    # bracket has closed and held. Until then `ready()` keeps returning None
    # and nothing changes.
    MAX_PLAUSIBLE = 12          # rounds; beyond this we have mis-inferred

    def record(self, slot, fired, turn_was_healthy=True):
        """The ONE way to report what happened to a slot. Returns a length if
        this observation closed the bracket, else None.

        A SINGLE ENTRY POINT ON PURPOSE. The first version of this had separate
        `observe_success` / `observe_failure` next to `use()`, and the order you
        called them in silently decided whether anything was learned: `use()`
        overwrites the last-used round, so calling it first made the gap zero
        and the upper bound was never recorded. A simulation of a 3-round
        cooldown ran eleven rounds and learned nothing, with the bracket stuck
        at (3, None) - an API where the wrong order fails silently is a bad API,
        so the bookkeeping is done here where the order cannot be got wrong.

        A FAILED use does not consume the skill, so `used_at` is only advanced
        when it actually fired.
        """
        used = self.used_at.get(slot)
        if fired:
            if used is not None and self.round - used > 0:
                gap = self.round - used
                hi = self._hi.get(slot)
                self._hi[slot] = gap if hi is None else min(hi, gap)
            self.used_at[slot] = self.round          # spent, so it starts now
        elif turn_was_healthy and used is not None:
            # It did not fire and the turn was otherwise fine, so the cooldown
            # must still have been running: it is longer than this gap.
            gap = self.round - used
            if gap >= 0:
                lo = self._lo.get(slot)
                self._lo[slot] = gap + 1 if lo is None else max(lo, gap + 1)
        return self._settle(slot)

    def _settle(self, slot):
        """Promote a closed bracket to a learned length. Returns it, or None."""
        lo, hi = self._lo.get(slot), self._hi.get(slot)
        if lo is None:
            # A COOLDOWN IS AT LEAST ONE ROUND, by definition - nothing can be
            # cast twice in the same round. So the lower bound is never really
            # unknown, and without this a 1-round cooldown could NEVER be
            # learned: it is ready again the very next round, so it never
            # fails, so no failure-derived bound is ever recorded and the
            # bracket sits at (None, 1) forever. Measured exactly that way -
            # cooldowns of 2, 3, 5 and 7 all converged while 1 stayed unknown.
            lo = 1
        if hi is None or lo != hi:
            return None
        if not (1 <= lo <= self.MAX_PLAUSIBLE):
            # Contradictory or absurd. Throw the bracket away rather than act
            # on it - the evidence was polluted by something we cannot see.
            self._lo.pop(slot, None)
            self._hi.pop(slot, None)
            return None
        self.learned[slot] = lo
        return lo

    def bracket(self, slot):
        """(low, high) of what the cooldown could still be. For logging."""
        return (self._lo.get(slot), self._hi.get(slot))

    def rounds_remaining(self, slot):
        cd = self.cooldowns.get(slot)
        used = self.used_at.get(slot)
        if cd is None or used is None:
            return 0
        return max(0, cd - (self.round - used))

    def learn_cooldown(self, slot, rounds):
        """Record an observed cooldown length so it becomes trackable."""
        self.cooldowns[slot] = int(rounds)


def parse_status_effects(text_lines):
    """Turn on-screen effect labels into (name, rounds_remaining).

    The trailing number is a DURATION IN ROUNDS, not a stack count: the client
    stores a `duration` on each buff/debuff and decrements it once per round,
    removing the effect at zero. `Blind(1)` means one round left.
    """
    import re
    out = []
    for line in text_lines:
        m = re.match(r"\s*(.+?)\s*\((\d+)\)\s*$", line)
        if m:
            out.append((m.group(1).strip(), int(m.group(2))))
    return out
