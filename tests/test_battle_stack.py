#!/usr/bin/env python3
"""Regression tests for the mission/battle stack, against RECORDED frames.

CLAUDE.md's standing rule: "Test guards against recorded data, not intuition."
DamageWatchdog already had two logic bugs that only surfaced when replayed
against the real measured HP sequence, so every assertion here is anchored to a
frame on disk or a number that was actually observed in game.

Run:  .venv/bin/python tests/test_battle_stack.py
"""
import glob
import logging
import os
import sys

import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))

import combat                                             # noqa: E402
from battle import SkillRotation                          # noqa: E402
from geometry import BattleGeometry, COMMAND, CMD_SIDE     # noqa: E402
from mission import REQUIRED_TEMPLATES, preflight, _find_all  # noqa: E402
from perceive import Template                              # noqa: E402

LOG = logging.getLogger("test")
logging.basicConfig(level=logging.CRITICAL)

FAILS = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def tpl(name):
    return Template(name, os.path.join(ROOT, "tpl", f"{name}.png"), threshold=0.88)


# --- 1. geometry: accept combat, reject everything else --------------------
def test_geometry_classification():
    print("\n[1] BattleGeometry.locate accepts combat frames only")
    ch, do = tpl("charge_btn"), tpl("dodge_btn")

    combat_frames = sorted(glob.glob(os.path.join(ROOT, "ref/combat/*.jpg")))
    # The epilogue frames are in ref/combat but have NO command bar - they are
    # cutscene stills. They must be rejected, which is the sharpest test of the
    # geometric cross-check: both templates DO match there, 340px apart at
    # mismatched scales, and only the pitch check rules them out.
    expect_reject = {"epi_50.jpg", "epi_51.jpg", "epi_52.jpg"}

    for p in combat_frames:
        name = os.path.basename(p)
        bgr = cv2.imread(p)
        g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        geo = BattleGeometry.locate(g, ch, do)
        if name in expect_reject:
            check(geo is None, f"{name}: rejected (no command bar)")
        else:
            check(geo is not None, f"{name}: accepted")

    for p in sorted(glob.glob(os.path.join(ROOT, "ref/raw/state_*.jpg"))):
        name = os.path.basename(p)
        g = cv2.cvtColor(cv2.imread(p), cv2.COLOR_BGR2GRAY)
        check(BattleGeometry.locate(g, ch, do) is None, f"{name}: rejected")


# --- 2. the two known geometries, and the invariant that links them --------
def test_two_geometries():
    print("\n[2] both capture geometries resolve, and pitch/scale is invariant")
    ch, do = tpl("charge_btn"), tpl("dodge_btn")
    expect = {"t0_after_first_attack.jpg": 0.46, "boss_t2.jpg": 0.545}
    ratios = []
    for name, want_scale in expect.items():
        g = cv2.cvtColor(cv2.imread(os.path.join(ROOT, "ref/combat", name)),
                         cv2.COLOR_BGR2GRAY)
        geo = BattleGeometry.locate(g, ch, do)
        check(geo is not None, f"{name}: located")
        if geo is None:
            continue
        check(abs(geo.scale - want_scale) < 0.02,
              f"{name}: scale {geo.scale:.3f} ~= {want_scale}")
        # Run sits one CMD_SIDE right of Charge; that distance over the scale is
        # the geometry-independent constant.
        run_x = geo.cmd("RN")[0]
        ch_x = geo.cmd("CH")[0]
        ratios.append((run_x - ch_x) / geo.scale)
    if len(ratios) == 2:
        spread = abs(ratios[0] - ratios[1]) / max(ratios)
        check(spread < 0.02,
              f"pitch/scale invariant across geometries "
              f"({ratios[0]:.1f} vs {ratios[1]:.1f}, {spread*100:.1f}% apart)")


# --- 3. the target ring, predicted across an 18% scale change --------------
def test_ring_cross_geometry():
    print("\n[3] target ring: 8 slots, correct team split, in BOTH geometries")
    ch, do = tpl("charge_btn"), tpl("dodge_btn")
    for name in ("t0_after_first_attack.jpg", "boss_t2.jpg"):
        bgr = cv2.imread(os.path.join(ROOT, "ref/combat", name))
        g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        geo = BattleGeometry.locate(g, ch, do)
        if geo is None:
            check(False, f"{name}: geometry")
            continue
        st = geo.ring_state(bgr)
        enemy = [k for k, v in st.items() if v == "enemy"]
        ally = [k for k, v in st.items() if v == "ally"]
        check(enemy == ["T1", "T2", "T3", "T4"],
              f"{name}: enemy side == T1..T4 (got {enemy})")
        check(ally == ["T5", "T6", "T7", "T8"],
              f"{name}: ally side == T5..T8 (got {ally})")
        check(geo.enemy_targets(bgr) == ["T1", "T2", "T3", "T4"],
              f"{name}: enemy_targets()")


# --- 4. DamageWatchdog on the real observed sequence ----------------------
def test_watchdog_recorded_sequence():
    print("\n[4] DamageWatchdog vs the sequence actually observed in game")
    # Measured live, from CLAUDE.md: the fight that could not be won.
    seq = [50.7, 43.0, 43.0, 43.0, 47.2]
    w = combat.DamageWatchdog(stall_turns=3)
    verdicts = [w.observe(v) for v in seq]
    check(verdicts[-1] in ("stalled", "regenerating"),
          f"final verdict on the unwinnable fight is an abort ({verdicts[-1]})")
    check(verdicts[-1] == "regenerating",
          f"HP going back UP is reported as regenerating (got {verdicts[-1]})")
    check(verdicts[0] == "continue", "a single reading never aborts")

    # A fight that IS progressing must never abort.
    w2 = combat.DamageWatchdog(stall_turns=3)
    good = [100.0, 88.0, 74.0, 61.0, 47.0, 30.0, 12.0]
    vs = [w2.observe(v) for v in good]
    check(all(v == "continue" for v in vs),
          "steady progress never triggers an abort")

    # Three turns parked at the same value is three turns of no progress, not
    # one. This is the specific bug the original implementation had.
    w3 = combat.DamageWatchdog(stall_turns=3)
    flat = [80.0, 60.0, 60.0, 60.0, 60.0]
    vf = [w3.observe(v) for v in flat]
    check(vf[-1] == "stalled", f"a flat plateau stalls (got {vf[-1]})")


# --- 5. rotate-on-resolve --------------------------------------------------
def test_skill_rotation():
    print("\n[5] SkillRotation: rotate-on-resolve and cooldown skipping")
    tracker = combat.CooldownTracker({"S1": 3})
    r = SkillRotation(["S1", "S2", "AT"], tracker, LOG)

    check(r.candidates() == ["S1", "S2", "AT"], "all slots offered initially")

    r.resolved("S1")
    check(r.order == ["S2", "AT", "S1"], "a resolved slot goes to the back")
    # S1 has a known 3-round cooldown and was used at round 0, so it is skipped
    # until three rounds have passed.
    check(r.candidates() == ["S2", "AT"], "known-cooling slot is withheld")
    for _ in range(3):
        tracker.next_round()
    check("S1" in r.candidates(), "slot returns once its cooldown elapses")

    # A slot with an UNKNOWN cooldown must still be offered - we never guess it
    # is unavailable - but it is demoted after firing.
    r2 = SkillRotation(["S5", "S6"], combat.CooldownTracker({}), LOG)
    r2.resolved("S5")
    check(r2.candidates() == ["S6", "S5"],
          "unknown-cooldown slot is offered again but demoted")

    r3 = SkillRotation(["S1", "S2"], combat.CooldownTracker({}), LOG)
    r3.failed("S1")
    check(r3.order == ["S2", "S1"], "a failed slot is demoted too")

    try:
        SkillRotation([], combat.CooldownTracker({}), LOG)
        check(False, "empty rotation must raise")
    except ValueError:
        check(True, "empty rotation raises rather than silently doing nothing")


# --- 6. command bar layout is a square, not a row -------------------------
def test_command_bar_layout():
    print("\n[6] command bar is a 2x2 block")
    check(COMMAND["AT"] == (0.0, -CMD_SIDE), "Attack is directly above Charge")
    check(COMMAND["RN"] == (CMD_SIDE, 0.0), "Run is directly right of Charge")
    check(COMMAND["DO"] == (CMD_SIDE, -CMD_SIDE), "Dodge is the diagonal")
    ch, do = tpl("charge_btn"), tpl("dodge_btn")
    g = cv2.cvtColor(cv2.imread(os.path.join(
        ROOT, "ref/combat/t0_after_first_attack.jpg")), cv2.COLOR_BGR2GRAY)
    geo = BattleGeometry.locate(g, ch, do)
    # Measured directly off that frame earlier: AT(419,393) DO(469,393)
    # CH(419,443) RN(469,443).
    check(geo.cmd("AT") == (419, 393), f"Attack at {geo.cmd('AT')} == (419,393)")
    check(geo.cmd("RN") == (469, 443), f"Run at {geo.cmd('RN')} == (469,443)")


# --- 7. preflight is honest about missing templates -----------------------
def test_preflight():
    print("\n[7] preflight names the templates that must be cut")
    have = {}
    missing = preflight(have, LOG)
    check(set(missing) == set(REQUIRED_TEMPLATES),
          "with no templates, every requirement is reported")
    check("mission_locked" in missing,
          "the padlock detector is a hard requirement")
    check("mission_success" in missing,
          "the success panel is a hard requirement")
    # The one that already exists in tpl/ but is known-unusable must still be
    # listed as needing a re-cut, not silently accepted.
    note = REQUIRED_TEMPLATES["cutscene_continue"][1]
    check("RE-CUT" in note.upper(),
          "click_to_continue is flagged as needing a re-cut")


# --- 8. multi-match with suppression --------------------------------------
def test_find_all_suppression():
    print("\n[8] _find_all collapses overlapping hits")
    frame = cv2.cvtColor(cv2.imread(os.path.join(
        ROOT, "ref/combat/t0_after_first_attack.jpg")), cv2.COLOR_BGR2GRAY)
    t = tpl("charge_btn")
    hits = _find_all(frame, t, max_hits=8)
    # There is exactly ONE charge button on screen. Without suppression,
    # matchTemplate's neighbourhood would yield a cluster of near-identical hits.
    check(len(hits) == 1, f"one charge button -> one hit (got {len(hits)})")
    if hits:
        check(abs(hits[0][0] - 419) < 12 and abs(hits[0][1] - 443) < 12,
              f"hit at {hits[0]} matches the measured (419,443)")


def main():
    for fn in (test_geometry_classification, test_two_geometries,
               test_ring_cross_geometry, test_watchdog_recorded_sequence,
               test_skill_rotation, test_command_bar_layout,
               test_preflight, test_find_all_suppression):
        fn()
    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
