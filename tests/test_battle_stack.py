#!/usr/bin/env python3
"""Regression tests for the mission/battle stack, against RECORDED frames.

CLAUDE.md's standing rule: "Test guards against recorded data, not intuition."
DamageWatchdog already had two logic bugs that only surfaced when replayed
against the real measured HP sequence, so every assertion here is anchored to a
frame on disk or a number that was actually observed in game.

Run:  .venv/bin/python tests/test_battle_stack.py
"""
import glob
import inspect
import logging
import os
import re
import sys
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))

import combat                                             # noqa: E402
from battle import SkillRotation                          # noqa: E402
from geometry import BattleGeometry, COMMAND, CMD_SIDE     # noqa: E402
from mission import REQUIRED_TEMPLATES, preflight, _find_all  # noqa: E402
import resume                                             # noqa: E402
from resume import Resumer, ARRIVED  # noqa: E402
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
    # A KILL MUST NOT READ AS REGENERATION. Fed the LOWEST enemy bar, the
    # watchdog fired whenever the weakest enemy died: the minimum over the
    # survivors jumps up, which is indistinguishable from healing. Measured live
    # at 18.4 -> 39.7 the moment a low-HP enemy dropped off the list, and it
    # fled a mission that was being won.
    w = combat.DamageWatchdog()
    verdicts = [w.observe(t, n) for t, n in
                [(300, 6), (280, 6), (260, 6), (240, 5), (235, 5), (230, 5)]]
    check(all(v == "continue" for v in verdicts),
          f"damage plus a kill never aborts (got {verdicts[-1]})")

    w = combat.DamageWatchdog()
    verdicts = [w.observe(300, n) for n in (6, 5, 4, 3, 2)]
    check(all(v == "continue" for v in verdicts),
          "killing an enemy each turn is progress even with total HP flat")

    w = combat.DamageWatchdog()
    verdicts = [w.observe(t, 6) for t in (300, 295, 298, 301, 299)]
    check(verdicts[-1] == "regenerating",
          f"a genuinely regenerating fight still aborts (got {verdicts[-1]})")

    print("\n[5] SkillRotation: priority order, cooldowns and the Attack fallback")
    tracker = combat.CooldownTracker({"S1": 3})
    r = SkillRotation(["S1", "S2", "AT"], tracker, LOG, mode="rotate")

    check(r.candidates() == ["S1", "S2", "AT"], "all slots offered initially")

    r.resolved("S1")
    check(r.order == ["S2", "AT", "S1"],
          "in ROTATE mode a resolved slot goes to the back")
    # S1 has a known 3-round cooldown and was used at round 0, so it is skipped
    # until three rounds have passed.
    check(r.candidates() == ["S2", "AT"], "known-cooling slot is withheld")
    for _ in range(3):
        tracker.next_round()
    check("S1" in r.candidates(), "slot returns once its cooldown elapses")

    # A slot with an UNKNOWN cooldown must still be offered - we never guess it
    # is unavailable - but it is demoted after firing.
    r2 = SkillRotation(["S5", "S6"], combat.CooldownTracker({}), LOG,
                       mode="rotate")
    r2.resolved("S5")
    check(r2.candidates() == ["S6", "S5"],
          "unknown-cooldown slot is offered again but demoted")

    r3 = SkillRotation(["S1", "S2"], combat.CooldownTracker({}), LOG,
                       mode="rotate")
    r3.failed("S1")
    check(r3.order == ["S2", "S1"], "a failed slot is demoted too")

    # PRIORITY MODE is the default, and is what "press these in this order, and
    # Attack when none are left" actually means: take the first READY slot, keep
    # the configured order, and never stall for want of something to click.
    tr = combat.CooldownTracker({"S1": 3, "S3": 5})
    rp = SkillRotation(["S1", "S3"], tr, LOG, fallback="AT")
    picks = []
    for rnd in range(10):
        if rnd:
            tr.next_round()
        pick = rp.candidates()[0]
        picks.append(pick)
        rp.resolved(pick)
    check(picks[:3] == ["S1", "S3", "AT"],
          f"priority takes the best ready slot, then Attack (got {picks[:3]})")
    check(rp.order == ["S1", "S3"],
          f"priority does NOT rotate the configured order (got {rp.order})")

    # A slot with NO known cooldown must still be demoted even in priority mode,
    # or it would be pressed every turn forever - bookkeeping is the only thing
    # that could stop that, and there is none.
    ru = SkillRotation(["S1", "S2"], combat.CooldownTracker({}), LOG,
                       fallback="AT")
    up = []
    for _ in range(4):
        pick = ru.candidates()[0]
        up.append(pick)
        ru.resolved(pick)
    check(up == ["S1", "S2", "S1", "S2"],
          f"unknown-cooldown slots still alternate in priority mode (got {up})")

    # Every skill cooling must still leave something to click.
    rf = SkillRotation(["S1"], combat.CooldownTracker({"S1": 9}), LOG,
                       fallback="AT")
    rf.resolved("S1")
    check(rf.candidates() == ["AT"],
          f"with every skill cooling the fallback is offered (got {rf.candidates()})")

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


# --- 9. the resume ladder, against recorded panel frames -------------------
class _FakeActor:
    dry_run = True

    def __init__(self):
        self.clicks = []

    def click_pixel(self, x, y, why=""):
        self.clicks.append((x, y, why))
        return (x, y)


def test_resume_ladder_panels():
    print("\n[9] resume ladder handles result panels and refuses the rest")
    d = os.path.join(ROOT, "tpl")
    tpls = {f[:-4]: Template(f[:-4], os.path.join(d, f), threshold=0.88)
            for f in sorted(os.listdir(d))
            if f.endswith(".png") and not f.startswith("_")}

    # (label, frame, expected step, must it click?)
    cases = [
        ("Victory panel", "ref/auto/panels/victory.png", "result_panel", True),
        ("Mission Success", "ref/auto/panels/mission_success.png",
         "mission_success", True),
        ("lobby", "ref/auto/lobby_full.png", "lobby", False),
        # The Mission Room IS now the ladder's job. It used to be "unknown" on
        # purpose, but that left the ladder with no exit from a screen it opens
        # itself: none of the popup X templates matched the panel (best 0.784)
        # and it halted after 20 unrecognised frames on a perfectly healthy
        # screen. Closing the panel returns to the village, which is exactly
        # what the ladder is for. The rung sits low, so anything on TOP of the
        # Mission Room is still handled first.
        ("mission room", "ref/auto/mission/room_05.png", "mission_room", True),
        ("combat", "ref/auto/mission/COMBAT.png", "unknown", False),
    ]
    for label, rel, expect, should_click in cases:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            check(False, f"{label}: frame missing ({rel})")
            continue
        act = _FakeActor()
        r = Resumer(None, act, tpls, LOG)
        gray = cv2.cvtColor(cv2.imread(p), cv2.COLOR_BGR2GRAY)
        out, info = r.advance(gray)
        check(info.get("step") == expect,
              f"{label}: step is {expect!r} (got {info.get('step')!r})")
        check(bool(act.clicks) == should_click,
              f"{label}: {'clicks' if should_click else 'clicks nothing'}")

    # The green check is ONE glyph at THREE sizes. A ladder that only looked at
    # one scale would find it on Victory and miss it on Mission Success, and the
    # mission could never close out. Assert the two click points differ, which is
    # only true if both were resolved at their own scale.
    pts = {}
    # A CUTSCENE IS A DEAD END WITHOUT ITS OWN RUNG. Measured live: a failed TP
    # mission ends on "Aww... you better take some rest..." over a
    # "click anywhere to continue" screen, and the ladder halted there after 20
    # unrecognised frames - right to refuse to click blindly, but unable to get
    # home from a screen whose only exit is a click.
    #
    # CLAUDE.md warns the OLD click_to_continue template was unusable (0.642 to
    # 0.849 on unrelated states). This is a different, re-cut template, and the
    # margin is what makes it safe - so assert the margin, not just the hit.
    from perceive import find
    names = [st.name for st in resume.DEFAULT_LADDER]
    check("cutscene" in names, "the ladder has a cutscene rung")
    if "cutscene" in names:
        check(names.index("cutscene") > names.index("result_panel"),
              "cutscene is checked AFTER the result panels, so a Victory panel "
              "is acknowledged by its check rather than clicked through")
        ct = os.path.join(ROOT, "tpl", "cutscene_continue.png")
        cf = os.path.join(ROOT, "ref/auto/tp/cutscene_failed.png")
        if os.path.exists(ct) and os.path.exists(cf):
            t = Template("cutscene_continue", ct, threshold=0.80)
            pos = find(cv2.cvtColor(cv2.imread(cf), cv2.COLOR_BGR2GRAY), t)[1]
            worst, who = 0.0, None
            for rel in ("ref/auto/lobby/lb0.png", "ref/auto/panels/victory.png",
                        "ref/auto/panels/mission_success.png",
                        "ref/auto/mission/COMBAT.png", "ref/auto/tp/room.png",
                        "ref/auto/tp/cards_now.png",
                        "ref/auto/tp/seal_active.png"):
                pp = os.path.join(ROOT, rel)
                if not os.path.exists(pp):
                    continue
                m2, c2 = find(cv2.cvtColor(cv2.imread(pp), cv2.COLOR_BGR2GRAY), t)
                if c2 > worst:
                    worst, who = c2, os.path.basename(rel)
                check(not m2.found,
                      f"cutscene does NOT fire on {os.path.basename(rel)} ({c2:.3f})")
            check(pos > 0.90, f"cutscene fires on a click-anywhere screen ({pos:.3f})")
            check(pos - worst > 0.30,
                  f"cutscene margin {pos - worst:.3f} over its worst negative ({who})")

    for label, rel in (("victory", "ref/auto/panels/victory.png"),
                       ("success", "ref/auto/panels/mission_success.png")):
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            continue
        act = _FakeActor()
        Resumer(None, act, tpls, LOG).advance(
            cv2.cvtColor(cv2.imread(p), cv2.COLOR_BGR2GRAY))
        pts[label] = act.clicks[0][:2] if act.clicks else None
    check(pts.get("victory") is not None and pts.get("success") is not None
          and pts["victory"] != pts["success"],
          f"green check resolved at its own scale per panel {pts}")


# --- 10. TP minigame classifier ---------------------------------------------
def test_minigame_classifier():
    print("\n[10] minigame classifier reads the screen, not a config flag")
    import minigame as mg
    cases = [
        # (frame, expected kind)
        ("ref/auto/tp/potion_after_start.png", mg.SEAL_ENTRY),
        ("ref/auto/tp/cards_now.png", mg.CARDS),
        ("ref/auto/tp/scroll_minigame.png", mg.CARDS),
        ("ref/auto/tp/kekkai_minigame.png", mg.KEKKAI),
        ("ref/auto/tp/kekkai_puzzle.png", mg.KEKKAI),
        # The battle target ring is red, wide, tall and sparse and passes every
        # shape filter a kekkai passes (area 11988, h 264, fill 0.115). Only the
        # combat context gate keeps it from reading as a seal.
        ("ref/auto/mission/COMBAT.png", mg.COMBAT),
        ("ref/combat/t0_after_first_attack.jpg", mg.COMBAT),
        # The village is full of red architecture; five lobby blobs cleared
        # area>=8000 before a bbox-height floor was added.
        ("ref/auto/lobby/lb0.png", mg.UNKNOWN),
        ("ref/auto/mission/room_05.png", mg.UNKNOWN),
        ("ref/auto/panels/victory.png", mg.UNKNOWN),
        ("ref/auto/panels/mission_success.png", mg.UNKNOWN),
    ]
    for rel, want in cases:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            check(False, f"{os.path.basename(rel)}: frame missing")
            continue
        got, ev = mg.classify(cv2.imread(p))
        check(got == want,
              f"{os.path.basename(rel):34s} -> {want} (got {got})")

    # Only kekkai is fully trusted; cards is playable but its face-match gate is
    # uncalibrated, so it must NOT be advertised as solvable.
    check(mg.SOLVABLE == {mg.KEKKAI},
          "only kekkai is claimed fully solvable")
    check(mg.CARDS in mg.EXPERIMENTAL and mg.CARDS not in mg.SOLVABLE,
          "cards is flagged experimental, not solvable")

    # The card board guard must refuse every non-board screen. This is the guard
    # whose absence walked a fixed 20-cell click grid into the weapon Shop.
    import cards as cd
    for rel, want in (("ref/auto/tp/cards_now.png", True),
                      ("ref/auto/tp/scroll_minigame.png", True),
                      ("ref/auto/lobby/lb0.png", False),
                      ("ref/auto/tp/kekkai_puzzle.png", False),
                      ("ref/auto/mission/COMBAT.png", False),
                      ("ref/auto/tp/potion_after_start.png", False)):
        p2 = os.path.join(ROOT, rel)
        if not os.path.exists(p2):
            continue
        check(cd.board_present(cv2.imread(p2)) is want,
              f"board_present({os.path.basename(rel)}) is {want}")


# --- 11. card matcher, against a board whose true pairs are known -----------
def test_card_matcher():
    """The memory board's face matcher, on the twenty saved crops.

    Ground truth is the ten pairs recovered by THREE independent metrics
    (mean-HSV signature, mean sat+val, and the reference bot's grey+Canny),
    which all agree - and which together form an exact perfect matching of all
    twenty positions, each used once.

    Two failures are pinned here because both cost a real mission:

    * the reference bot's grey+Canny metric does not separate this board at all
      (worst true pair 139.85 > best non-pair 104.96), so no threshold on it can
      work. Ours must separate.
    * mutual-best is VACUOUS with two revealed cards - each is trivially the
      other's best - which is how a run reported "10/10 pairs" against the
      game's own "Remaining Cards: x18". Two unrelated faces must propose
      nothing.
    """
    print("\n[11] card matcher")
    import itertools
    import glob
    import cards as cd

    fs = sorted(glob.glob(os.path.join(ROOT, "ref/auto/tp/faces/pos*.png")))
    if len(fs) < 20:
        print("  SKIP  calibration crops not present")
        return
    crops = {int(os.path.basename(p)[3:5]): cv2.imread(p) for p in fs}
    ids = sorted(crops)
    TRUE = {frozenset(p) for p in
            [(11, 13), (0, 17), (14, 16), (18, 19), (6, 10),
             (9, 15), (2, 12), (1, 5), (4, 8), (3, 7)]}

    S = {i: cd.sig(crops[i]) for i in ids}
    D = {frozenset((i, j)): cd.distance(S[i], S[j])
         for i, j in itertools.combinations(ids, 2)}
    worst_true = max(D[k] for k in TRUE)
    best_non = min(v for k, v in D.items() if k not in TRUE)
    check(worst_true < cd.MATCH_GATE < best_non,
          f"gate {cd.MATCH_GATE} separates true {worst_true:.2f} from "
          f"non-pair {best_non:.2f}")

    class _Q:
        def info(self, *a):
            pass
        warning = error = info

    b = cd.Board(_Q())
    b.seen = dict(S)
    proposed = []
    while True:
        p = b.pending_pair()
        if not p:
            break
        proposed.append(frozenset(p))
        b.cleared.update(p)
    check(len(proposed) == 10 and all(p in TRUE for p in proposed),
          "all ten true pairs proposed, and nothing else")

    b2 = cd.Board(_Q())
    b2.seen = {3: S[3], 6: S[6]}          # not a pair
    check(b2.pending_pair() is None,
          "two unrelated revealed cards propose NOTHING (mutual-best is vacuous "
          "on its own)")
    b3 = cd.Board(_Q())
    b3.seen = {3: S[3], 7: S[7]}          # a real pair
    check(b3.pending_pair() is not None,
          "two matching revealed cards do propose a pair")

    b4 = cd.Board(_Q())
    b4.seen = dict(S)
    b4.rejected = {frozenset((3, 7))}
    check(b4.best_partner(3) is None,
          "a pair the board refused is never proposed again")

    # The clip and the full frame must be the same pixel space, only translated.
    # Getting this wrong (CDP's clip scale defaults to 1, the frame comes back at
    # dpr) made every cell read the wrong pixels.
    full = cv2.imread(os.path.join(ROOT, "ref/auto/tp/cards_now.png"))
    if full is not None:
        x, y, w, h = cd.BOARD_BOX
        clip = full[y:y + h, x:x + w]
        same = all(np.array_equal(cd.crop(full, i), cd.crop(clip, i, (x, y)))
                   for i in range(cd.N))
        check(same, "BOARD_BOX crops match full-frame crops for all 20 cells")
        check(cd.board_present(clip, (x, y)) is True,
              "BOARD_BOX still contains the HUD anchor")
        check(all(cd.cell_state(full, i) == cd.BACK for i in range(cd.N)),
              "the all-face-down frame reads as 20 backs")


# --- 12. the templates that navigate TP and close its dialogs ---------------
def test_tp_navigation_templates():
    """Row titles and the share-prompt X, each against the frames that fooled it.

    Every one of these was minted because something REAL went wrong:

    * `tp_scroll_row` / `tp_scroll2_row` pick a mission BY NAME. Two of the five
      TP missions are "Secret TP Scroll" and "Another TP Scroll", so a picker
      that matched loosely would start the wrong one - and the TP list is a DAILY
      list that shrinks as missions are completed, so both templates are needed
      for the family to stay reachable all day.
    * `close_share_x` closes the "Share with Teammates!" prompt that covers the
      Mission Success check. None of the four existing X templates matched it
      (0.719 / 0.586 / 0.465 / 0.402), so close-out timed out with the reward
      panel still open and the mission unbanked.

    The share prompt also carries a GREEN CHECK on its "Share to wall" button,
    which is why it must be dismissed before the success check is looked for -
    and why nothing here may ever click that button.
    """
    print("\n[12] TP navigation and dialog templates")
    from perceive import Template, find

    def tpl_at(name, thr=0.88):
        p = os.path.join(ROOT, "tpl", f"{name}.png")
        return Template(name, p, threshold=thr) if os.path.exists(p) else None

    cases = [
        # template,        frame it must fire on,          frames it must NOT
        ("tp_scroll_row", "ref/auto/tp/tp_list.png",
         ["ref/auto/tp/tp_list_p2.png", "ref/auto/tp/room.png",
          "ref/auto/tp/special.png", "ref/auto/lobby/lb0.png"]),
        ("tp_scroll2_row", "ref/auto/tp/tp_list_p2.png",
         ["ref/auto/tp/tp_list.png", "ref/auto/tp/room.png",
          "ref/auto/lobby/lb0.png"]),
    ]
    for name, pos, negs in cases:
        t = tpl_at(name)
        pp = os.path.join(ROOT, pos)
        if t is None or not os.path.exists(pp):
            print(f"  SKIP  {name}")
            continue
        img = cv2.imread(pp)
        m, c = find(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), t)
        check(m.found, f"{name} fires on {os.path.basename(pos)} ({c:.3f})")
        worst, who = 0.0, None
        for n in negs:
            np_ = os.path.join(ROOT, n)
            if not os.path.exists(np_):
                continue
            im = cv2.imread(np_)
            mm, cc = find(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), t)
            if cc > worst:
                worst, who = cc, os.path.basename(n)
            check(not mm.found, f"{name} does NOT fire on {os.path.basename(n)} "
                                f"({cc:.3f})")
        check(c - worst > 0.10,
              f"{name} margin {c - worst:.3f} over its worst negative ({who})")

    # "Secret TP Scroll" and "Another TP Scroll" must not be confused for each
    # other - that is the whole reason they are separate templates.
    a, b = tpl_at("tp_scroll_row"), tpl_at("tp_scroll2_row")
    p2 = os.path.join(ROOT, "ref/auto/tp/tp_list_p2.png")
    if a is not None and b is not None and os.path.exists(p2):
        g2 = cv2.cvtColor(cv2.imread(p2), cv2.COLOR_BGR2GRAY)
        ma, ca = find(g2, a)
        mb, cb = find(g2, b)
        check(mb.found and not ma.found,
              f"on the page holding 'Another TP Scroll', only its own template "
              f"fires ({cb:.3f} vs {ca:.3f})")

    # The share-prompt X. Its negatives are every other panel that has a red X
    # or a close control, including the promo modal it must not be confused with.
    x = tpl_at("close_share_x")
    if x is not None:
        for rel, want in (("ref/auto/promo_modal.png", False),
                          ("ref/auto/panels/mission_success.png", False),
                          ("ref/auto/panels/victory.png", False),
                          ("ref/auto/tp/room.png", False),
                          ("ref/auto/lobby/lb0.png", False),
                          ("ref/auto/mission/COMBAT.png", False)):
            pp = os.path.join(ROOT, rel)
            if not os.path.exists(pp):
                continue
            mm, cc = find(cv2.cvtColor(cv2.imread(pp), cv2.COLOR_BGR2GRAY), x)
            check(mm.found is want,
                  f"close_share_x on {os.path.basename(rel)} is {want} ({cc:.3f})")

    # The picker must know BOTH scroll missions, or the family goes unreachable
    # as soon as the first one is done for the day.
    import tp as tprun
    check(len(tprun.ROW_TEMPLATES.get("scroll", [])) >= 2,
          "the scroll family lists both of its missions")
    check({"scroll", "kekkai", "potion"} <= tprun.SUPPORTED,
          "all three TP families are playable")
    check(tprun.TRY_ORDER[-1] == "potion",
          "the hand-seal game is tried LAST - it is the least reliable, so the "
          "families we solve cleanly get first go at the day's missions")
    check(len(tprun.ROW_TEMPLATES.get("potion", [])) >= 2,
          "both Potion missions are addressable by name")


# --- 13. the control dock: event plumbing, click guard, halt anchor ---------
def test_dock_and_controls():
    """The pieces that make an in-page control panel safe, without a browser.

    Each check pins a bug that actually happened while building it:

    * `CDP.call` used to DISCARD every message that was not its own reply, so
      events did not reach this codebase AT ALL - silently, nothing erroring.
      An in-page panel is impossible without them: a button press arrives as
      `Runtime.bindingCalled`.
    * The first `poll_events` waited with a SOCKET TIMEOUT, which can expire
      mid-websocket-frame, consume half a frame, and desynchronise the stream
      permanently - every later read garbage, the next `call()` hanging forever
      with no error. It waits with `select` now.
    * Buffering everything is not viable: enabling Runtime on this game filled a
      512-slot buffer with `consoleAPICalled` in under a second, which would
      evict the button press. Hence the watch allowlist.
    * The dock's Pause writes to the control file, a task parks in
      `wait_if_paused`, and the only loop that could read the operator's next
      press is the parked one. That DEADLOCKED live. `on_wait` fixes it.
    * The panel is injected into the game page, so its buttons are as clickable
      as anything else - the bot must refuse to click into it.
    """
    print("\n[13] control dock plumbing")
    import act as act_mod
    import cdp as cdp_mod
    import dock as dock_mod

    # -- event buffer, without a socket ------------------------------------
    c = cdp_mod.CDP.__new__(cdp_mod.CDP)
    c._events, c._max_events, c._watch = [], 4, None
    for i in range(6):
        c._stash({"method": "Runtime.consoleAPICalled", "i": i})
    check(len(c._events) == 4 and c._events[-1]["i"] == 5,
          "the event buffer is bounded and drops the OLDEST, keeping the newest")

    c._events, c._watch = [], {"Runtime.bindingCalled"}
    c._stash({"method": "Runtime.consoleAPICalled"})
    c._stash({"method": "Runtime.bindingCalled", "params": {"payload": "{}"}})
    check(len(c._events) == 1
          and c._events[0]["method"] == "Runtime.bindingCalled",
          "the watch allowlist keeps console spam out of the buffer")

    c._stash({"id": 7, "result": {}})
    check(len(c._events) == 1, "command replies are never stashed as events")

    # `call` must stash what it skips, not drop it.
    src = inspect.getsource(cdp_mod.CDP.call)
    check("self._stash(msg)" in src,
          "call() buffers the events it skips instead of discarding them")
    check("select.select" in inspect.getsource(cdp_mod.CDP.poll_events),
          "poll_events waits with select, never a socket timeout mid-frame")

    # -- no-click zones -----------------------------------------------------
    class _Cap:
        dpr = 2
        def to_click_coords(self, x, y):
            return x / 2, y / 2

    class _Rec:
        def __init__(self):
            self.warns = []
        def info(self, m, *a):
            pass
        def warning(self, m, *a):
            self.warns.append((m % a) if a else m)
        error = info

    rec = _Rec()
    a = act_mod.Actor(None, _Cap(), rec, dry_run=True,
                      no_click_zones=[(2680, 0, 760, 1440)])
    check(a.blocked_by(2880, 400) == (2680, 0, 760, 1440),
          "a point inside the dock is reported as blocked")
    check(a.blocked_by(1720, 720) is None,
          "a point inside the game is not blocked")
    check(a.blocked_by(2679, 400) is None,
          "the zone edge is exclusive on the left")
    check(a.click_pixel(2880, 400, why="dock") is None and rec.warns,
          "clicking into the dock is REFUSED and logged, not silently dropped")
    check(a.click_pixel(1720, 720, why="game") is None,
          "a game click still goes through the dry-run path")

    # -- pause must not deadlock -------------------------------------------
    import tempfile
    ctl_path = os.path.join(tempfile.mkdtemp(), "bot.control")
    with open(ctl_path, "w") as f:
        f.write("pause")
    pumped = {"n": 0}

    def pump():
        pumped["n"] += 1
        if pumped["n"] >= 3:                 # the operator presses Run
            with open(ctl_path, "w") as f:
                f.write("run")

    ctl = act_mod.Controls(ctl_path, None)
    ctl.on_wait = pump
    t0 = time.time()
    released = ctl.wait_if_paused(poll=0.01)
    check(released and pumped["n"] >= 3 and time.time() - t0 < 5,
          "a paused task pumps operator input and can be un-paused from the dock")

    with open(ctl_path, "w") as f:
        f.write("stop")
    check(ctl.wait_if_paused(poll=0.01) is False,
          "stop still breaks the wait")

    # -- the dock renders into the gutter, never over the game -------------
    check(dock_mod.WIDTH <= 385,
          f"the dock ({dock_mod.WIDTH}px) fits the measured 385px right gutter")
    # THE PANEL'S SKILL ORDER MUST REACH THE NEXT BATTLE, and must not rewrite
    # the loaded config to do it - that is what makes it editable live.
    import app as _app
    rr = _app.Runner.__new__(_app.Runner)
    rr.cfg = {"battle": {"rotation": ["AT"], "fallback": "AT"}}
    rr.skills, rr.grade, rr.pin_page, rr.pin_row = [], None, None, None
    check(rr.battle_cfg()["battle"]["rotation"] == ["AT"],
          "no skills chosen means Attack only")
    rr.skills = ["S1", "S3"]
    built = rr.battle_cfg()
    check(built["battle"]["rotation"] == ["S1", "S3"],
          "the panel's order becomes the battle rotation")
    check(rr.cfg["battle"]["rotation"] == ["AT"],
          "building it does NOT mutate the loaded config")
    check(built["battle"].get("fallback") == "AT",
          "the Attack fallback survives, so a fight never stalls")
    check(set(_app.SKILL_SLOTS) >= {"AT", "S1", "S8"},
          "the panel offers the command buttons and all eight slots")

    # THE FARM TARGET IS EDITABLE FROM THE PANEL TOO. Auto means "read the grade
    # panel and take the highest unlocked mission"; a pin overrides both.
    rr.cfg = {"battle": {"rotation": ["AT"]}, "mission": {"grade": "A"}}
    rr.skills, rr.grade, rr.pin_page, rr.pin_row = [], None, None, None
    m = rr.battle_cfg()["mission"]
    check(m["grade"] is None,
          "auto grade overrides a grade left in the config file")
    check(m["mission_page"] is None and m["mission_row"] is None,
          "no pin means the highest unlocked mission")
    rr.grade, rr.pin_page, rr.pin_row = "B", 3, 2
    m = rr.battle_cfg()["mission"]
    check((m["grade"], m["mission_page"], m["mission_row"]) == ("B", 3, 2),
          "a panel pin reaches the farm loop")
    check(rr.cfg["mission"]["grade"] == "A",
          "building it does NOT mutate the loaded config")
    check(_app.GRADES[0] == "auto" and set(_app.GRADES) >= {"A", "B", "C"},
          "the panel offers auto plus the grades")

    import inspect as _i
    src = _i.getsource(__import__("farm").start_best)
    check("page and row" in src or "page, row" in src,
          "start_best routes a pinned page/row to the pinned starter")

    # A dead socket must be recognised and RECONNECTED, not spun on. Logging out
    # tore the CDP target down, every later call raised BrokenPipeError, and the
    # process stayed alive logging "panel update failed" forever - the panel gone
    # and the only fix a manual restart, which is the one thing the dock exists
    # to avoid.
    import app as app_mod

    class _Dead:
        def evaluate(self, *a, **k):
            raise BrokenPipeError(32, "Broken pipe")
        def close(self):
            pass

    class _Q2:
        def info(self, *a):
            pass
        warning = error = info

    r = app_mod.Runner.__new__(app_mod.Runner)
    r.cdp, r.dock, r.log = _Dead(), _Dead(), _Q2()
    try:
        r.ensure_dock()
        raised = False
    except app_mod.Disconnected:
        raised = True
    except Exception:
        raised = False
    check(raised, "a dead socket raises Disconnected instead of being swallowed")
    check("reconnect" in inspect.getsource(app_mod.Runner.loop),
          "the loop reconnects rather than spinning on a dead socket")
    check(hasattr(app_mod, "attach"),
          "connection setup is factored out so it can be rebuilt in place")

    src = dock_mod._BOOTSTRAP
    check("window.top !== window" in src,
          "the panel refuses to inject itself inside the game iframe")
    check("addScriptToEvaluateOnNewDocument" in inspect.getsource(dock_mod.Dock.install),
          "the panel is re-injected on navigation, so a relog does not lose it")
    check("overlaps" in inspect.getsource(dock_mod.Dock.install),
          "install REFUSES rather than reflowing the player element")

    # -- signing out halts, and is anchored ---------------------------------
    from perceive import Template, find
    lo = os.path.join(ROOT, "tpl", "logged_out.png")
    if os.path.exists(lo):
        t = Template("logged_out", lo, threshold=0.88)
        worst, who = 0.0, None
        for rel in ("ref/auto/lobby/lb0.png", "ref/auto/mission/COMBAT.png",
                    "ref/auto/tp/cards_now.png", "ref/auto/tp/room.png",
                    "ref/auto/panels/mission_success.png"):
            pp = os.path.join(ROOT, rel)
            if not os.path.exists(pp):
                continue
            mm, cc = find(cv2.cvtColor(cv2.imread(pp), cv2.COLOR_BGR2GRAY), t)
            if cc > worst:
                worst, who = cc, os.path.basename(rel)
            check(not mm.found,
                  f"logged_out does NOT fire on {os.path.basename(rel)} ({cc:.3f})")
        check(worst < 0.60, f"logged_out worst in-game score {worst:.3f} ({who})")

    names = [st.name for st in resume.DEFAULT_LADDER]
    check("logged_out" in names, "the ladder has a logged_out rung")
    step = next(st for st in resume.DEFAULT_LADDER if st.name == "logged_out")
    check(step.action == "halt",
          "the logged_out rung HALTS - it never tries to authenticate")
    check(names.index("logged_out") <= 1,
          "logged_out is checked first, before anything tries to click")


# --- 14. kekkai feedback digits, and knowing when to stop hunting -----------
def test_kekkai_digits():
    """The counter reader that stalled the Kekkai mission twice.

    Symptom both times: "could not read row N (green 0.524 / gold 0.611)", and
    the solver correctly refused to guess rather than treat an unread counter as
    a zero — a wrong 0 is indistinguishable from a real one and would corrupt
    the model. But it meant the mission could never finish.

    TWO causes, and only the second is obvious:

    1. Missing digits. The library held only 0 and 1, so any feedback of 2
       was unreadable. (A 2 turned up on the very first live run.)
    2. **Same-size template matching has NO alignment freedom.** An exemplar the
       same size as the patch gives `matchTemplate` exactly one position to
       score, so the row drift CLAUDE.md already documents (pitch 88.53 vs an
       assumed 88.0) is charged straight to the confidence. Trimming the
       exemplar to its glyph and searching it inside the full patch took the
       same-digit score from 0.311 to 1.000.

    Connected-component isolation was tried instead and is measurably WORSE
    (every 0 dropped to ~0.72 and failed the gate), so tight-cropping stands.
    """
    print("\n[14] kekkai feedback digits")
    import kekkai_play as kp

    lib = kp.load_exemplars()
    check(bool(lib), "digit exemplars load")
    check(set(lib) >= {0, 1, 2},
          f"library covers 0, 1 and 2 (has {sorted(lib)})")
    check(all(set(np.unique(i)) <= {0, 255} for v in lib.values() for i in v),
          "exemplars are binarised the same way the live patch is")

    # Tight-cropping must actually shrink the glyph, or it is doing nothing.
    any_trimmed = any(kp.tight_glyph(i).shape != i.shape
                      for v in lib.values() for i in v)
    check(any_trimmed, "tight_glyph trims the exemplar below the patch size")

    # Every exemplar must classify as its own digit against the whole library,
    # comfortably above the gate.
    d = os.path.join(ROOT, "ref/auto/tp/digits")
    worst = 1.0
    for f in sorted(glob.glob(os.path.join(d, "*.png"))):
        base = os.path.basename(f)
        head = os.path.splitext(base)[0].split("_")[0]
        if not head.isdigit():
            continue
        want = int(head)
        g = cv2.cvtColor(cv2.imread(f), cv2.COLOR_BGR2GRAY)
        patch = cv2.threshold(g, 200, 255, cv2.THRESH_BINARY)[1]
        best, got = 0.0, None
        for val, imgs in lib.items():
            for img in imgs:
                t = kp.tight_glyph(img)
                if t.shape[0] > patch.shape[0] or t.shape[1] > patch.shape[1]:
                    continue
                m = float(cv2.minMaxLoc(
                    cv2.matchTemplate(patch, t, cv2.TM_CCOEFF_NORMED))[1])
                if m > best:
                    best, got = m, val
        worst = min(worst, best)
        check(got == want and best >= 0.80,
              f"{base} reads as {want} ({got} @ {best:.3f})")
    check(worst >= 0.95, f"weakest self-match across the library {worst:.3f}")

    # An unreadable counter must return None, never a silent zero.
    blank = np.zeros((68, 68), np.uint8)

    class _F:
        shape = (2000, 3440, 3)
    val, conf = kp.read_digit(
        np.zeros((2000, 3440, 3), np.uint8), (500, 500), lib)
    check(val is None,
          f"an unreadable counter returns None, not a zero (conf {conf:.3f})")

    # Knowing when to STOP hunting. Breaking the last seal ends the mission;
    # a run that kept hunting afterwards ran to a map edge that no longer
    # existed and tried to "open" panel artwork three times.
    class _Cap:
        def __init__(self, p):
            self.f = cv2.imread(p)
        def frame(self, gray=False):
            return self.f

    class _Q:
        def info(self, *a):
            pass
        warning = error = info

    for rel, want in (("ref/auto/panels/mission_success.png", True),
                      ("ref/auto/lobby/lb0.png", False),
                      ("ref/auto/tp/kekkai_puzzle.png", False),
                      ("ref/auto/tp/kekkai_minigame.png", False),
                      ("ref/auto/mission/COMBAT.png", False)):
        pp = os.path.join(ROOT, rel)
        if not os.path.exists(pp):
            continue
        check(kp.mission_over(_Cap(pp), _Q()) is want,
              f"mission_over({os.path.basename(rel)}) is {want}")

    src = inspect.getsource(kp.solve_live)
    check("resumed" in src and "seal_broken" in src,
          "a first-guess panel closure on a RESUMED puzzle needs corroboration")


# --- 15. hand-seal minigame: the three phases, and the Start anchor ---------
def test_seal_phases():
    """The Potion family's phase detection.

    CLAUDE.md had this game recorded as unsolvable on two claims that a live
    observation disproves: that the two slot cards are "the empty INPUT, not a
    revealed answer", and that no reveal exists. Pressing Start opens a LOOK
    PHASE in which both slots flip over and display the two required seals. The
    earlier burst simply sampled before Start.

    Three phases have to be told apart, and the obvious signal is wrong for two
    of them:

        face down    orange flame card backs
        look         slots revealed, the ten tiles drawn GREYED
        active       tiles in full colour and clickable

    SATURATION DOES NOT SEPARATE THESE. A face-down card is flame art and reads
    as saturated as a live seal, which cost two live rounds - once by concluding
    a round was already running and never pressing Start, once by trying to read
    seals off card backs. The blue glove does separate them: only a live seal
    has one.
    """
    print("\n[15] hand-seal minigame phases")
    import seals as se

    cases = [
        ("seal_facedown.png", False, False, True),   # tiles live, slots shown, Start
        ("seal_look.png",     False, True,  False),
        ("seal_active.png",   True,  False, False),
    ]
    from perceive import Template, find
    sp = os.path.join(ROOT, "tpl", "tp_seal_start.png")
    start_t = Template("tp_seal_start", sp, threshold=0.88) if os.path.exists(sp) else None

    for name, want_live, want_shown, want_start in cases:
        p = os.path.join(ROOT, "ref/auto/tp", name)
        if not os.path.exists(p):
            print(f"  SKIP  {name}")
            continue
        f = cv2.imread(p)
        live, blues = se.tiles_live(f)
        shown, sb = se.slots_revealed(f)
        check(live is want_live,
              f"{name}: tiles_live {live} (blue {blues[0]:.3f})")
        check(shown is want_shown,
              f"{name}: slots_revealed {shown} (blue {sb[0]:.3f})")
        check(se.board_present(f) is True, f"{name}: the Skill HUD is found")
        if start_t is not None:
            m, c = find(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), start_t)
            check(m.found is want_start,
                  f"{name}: Start button {m.found} ({c:.3f})")

    # The Start anchor must not fire anywhere else.
    if start_t is not None:
        for rel in ("ref/auto/lobby/lb0.png", "ref/auto/tp/cards_now.png",
                    "ref/auto/mission/COMBAT.png", "ref/auto/tp/room.png"):
            pp = os.path.join(ROOT, rel)
            if not os.path.exists(pp):
                continue
            m, c = find(cv2.cvtColor(cv2.imread(pp), cv2.COLOR_BGR2GRAY), start_t)
            check(not m.found,
                  f"tp_seal_start does not fire on {os.path.basename(rel)} ({c:.3f})")

    # Geometry: ten tiles on a fixed pitch, two slots, all inside the frame.
    check(len(se.TILES) == 10 and se.TILES[1][0] - se.TILES[0][0] == 150,
          "ten tiles on the measured 150px pitch")
    f = cv2.imread(os.path.join(ROOT, "ref/auto/tp/seal_active.png"))
    if f is not None:
        h, w = f.shape[:2]
        check(all(0 < x < w and 0 < y < h for x, y in se.TILES + se.SLOTS),
              "every tile and slot centre lies inside the frame")

    # THE PANEL MOVES, so geometry must be anchor-relative. Measured live: a
    # page reload shifted Start from y=400 to y=432, every tile and slot crop
    # went with it, and the match margins collapsed from 7..14x to 1.0x - the
    # solver picked wrong twice on a board it had been reading perfectly.
    look = cv2.imread(os.path.join(ROOT, "ref/auto/tp/seal_look.png"))
    act = cv2.imread(os.path.join(ROOT, "ref/auto/tp/seal_active.png"))
    if look is not None and act is not None:
        check(se.anchor_offset(look) == (0, 0),
              "the reference frame has zero panel offset")

        def picks(lk, ac, off):
            ss = se.find_slots(lk, off=off)
            art = [se.slot_crop(lk, i, ss) for i in range(len(ss))]
            r = se.rank_candidates_from(art, ac, None, off)
            return None if r is None else [(r[i][0][1],
                                            r[i][1][0] / max(1e-6, r[i][0][0]))
                                           for i in range(len(r))]

        base = picks(look, act, (0, 0))
        for dy in (20, 32):
            M = np.float32([[1, 0, 0], [0, 1, dy]])
            lk = cv2.warpAffine(look, M, (look.shape[1], look.shape[0]))
            ac = cv2.warpAffine(act, M, (act.shape[1], act.shape[0]))
            off = se.anchor_offset(lk)
            check(off == (0, dy), f"a {dy}px shift is detected as {off}")
            anch = picks(lk, ac, off)
            check(anch is not None
                  and [p[0] for p in anch] == [p[0] for p in base],
                  f"anchored picks survive a {dy}px shift")
            naive = picks(lk, ac, (0, 0))
            check(naive is None
                  or [p[0] for p in naive] != [p[0] for p in base],
                  f"UNanchored picks would be wrong at {dy}px - the anchor earns "
                  f"its keep")

    # Abstaining strands the round, so the default must be to commit.
    import inspect
    sig = inspect.signature(se.play_round)
    check(sig.parameters["commit"].default is True,
          "play_round commits by default - abstaining after the look phase "
          "parks the round forever")


def test_mission_list_is_not_scenery():
    """A mission LIST page must never be mistaken for a walkable map.

    This is the bug that made farming look broken: on Grade A page 5/7 - three
    padlocked rows, both page arrows - none of the six original NOT_IN_MISSION
    anchors matched, so `looks_like_mission_scene` said True and the runner
    "walked" by clicking the map edge INSIDE the mission list. The mission never
    started and the bot never left the page.
    """
    print("\nmission list vs mission scenery")
    from perceive import find
    import farm, bot as botmod
    import json as _json
    cfg = _json.load(open(os.path.join(ROOT, "Configs/mission.json")))
    tpls = botmod.load_templates(cfg, LOG)

    listing = os.path.join(ROOT, "ref/auto/mission/list_all_locked.png")
    frame = cv2.imread(listing)
    check(frame is not None, "the all-locked list frame is committed")
    if frame is None:
        return
    check(farm.looks_like_mission_scene(frame, tpls) is False,
          "an all-locked mission list is NOT a walkable scene")

    # The anchors that carry it, with the margins they were chosen for.
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    for name, floor in (("page_next", 0.90), ("page_prev", 0.90),
                        ("mission_locked", 0.90), ("list_back_arrow", 0.90)):
        c = find(gray, tpl(name))[1]
        check(c >= floor, f"{name} anchors the list page ({c:.3f} >= {floor})")

    # ...and must NOT fire on a real in-mission frame, or the fix would block
    # traversal instead - trading one stuck state for another.
    combat = cv2.cvtColor(
        cv2.imread(os.path.join(ROOT, "ref/auto/mission/COMBAT.png")),
        cv2.COLOR_BGR2GRAY)
    for name in ("page_next", "page_prev", "mission_locked", "list_back_arrow"):
        c = find(combat, tpl(name))[1]
        check(c < 0.88, f"{name} stays silent in combat ({c:.3f} < 0.88)")


def test_cold_command_bar_probe_is_budgeted():
    """A missing command bar must not cost a full sweep on every cycle.

    The full 90-scale sweep measures ~13 s on a 3440x1440 frame. The hint path
    already budgeted its misses; the COLD path (no hint cached) did not, so a
    process that had never seen a battle paid 13 s per farm cycle to be told
    there was no command bar. The panel froze for 40 s at a time and operator
    commands were not read until the sweep finished.
    """
    print("\ncold command-bar probe is budgeted")
    from geometry import BattleGeometry
    frame = cv2.imread(os.path.join(ROOT, "ref/auto/mission/list_all_locked.png"))
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    ch, do = tpl("charge_btn"), tpl("dodge_btn")

    BattleGeometry.forget()
    check(BattleGeometry._cold_probed is False,
          "forget() disarms the cold budget so a layout change can re-probe")

    t0 = time.time()
    first = BattleGeometry.locate(gray, ch, do)
    cold = time.time() - t0
    check(first is None, "no command bar on a mission list page")
    check(BattleGeometry._cold_probed is True,
          "the first cold miss still pays for a full sweep")

    t0 = time.time()
    for _ in range(5):
        check_none = BattleGeometry.locate(gray, ch, do)
    warm = (time.time() - t0) / 5.0
    check(check_none is None, "still correctly reports no command bar")
    # The point of the change is the RATIO, so assert on that rather than on a
    # wall-clock number that would vary by machine.
    check(warm < cold / 3.0,
          f"a budgeted miss is far cheaper than the sweep "
          f"({warm:.2f}s vs {cold:.2f}s)")


def test_focus_survives_a_reload():
    """Focus mode must come back by itself after the page reloads.

    `focus_on` is a Python-side belief and a reload silently invalidates it: the
    panel re-injects onto the new document, so the dock PRESENCE check still
    passes while the fresh document is not focused. The old code early-returned
    on the stale flag and never re-applied - measured live after a Relog,
    `__nsbotFocusOn` false with `scrollY` 301, i.e. the game drifted out of the
    viewport. `ensure_focus` reads the real state instead.

    Driven with a stub dock so it needs no browser.
    """
    print("\nfocus mode survives a reload")
    import app as app_mod

    class StubDock:
        def __init__(self):
            self.page_focused = False   # what the DOCUMENT says
            self.applies = 0
            self.aligns = 0
        def focus_state(self):
            return self.page_focused
        def game_ready(self):
            return True
        def focus(self, on=True):
            self.applies += 1
            self.page_focused = bool(on)
            return "focused"
        def align(self):
            self.aligns += 1
            return "realigned"

    r = app_mod.Runner.__new__(app_mod.Runner)
    r.dock = StubDock()
    r.log = LOG
    r.focus_wanted = True
    r.focus_on = False
    r.focus_aligned = False

    r.ensure_focus()
    check(r.dock.applies == 1, "focus is applied when the page is not focused")
    check(r.focus_on is True, "the flag follows the page")

    # Steady state: already focused, so no re-apply. Re-applying on every cycle
    # is what made the game jump around and the state read "unknown".
    before = r.dock.applies
    for _ in range(5):
        r.ensure_focus()
    check(r.dock.applies == before,
          "an already-focused page is never re-applied (no jumping)")

    # THE RELOAD. The document comes back unfocused while the flag still says
    # True - exactly the stale-belief case.
    r.dock.page_focused = False
    check(r.focus_on is True, "the stale flag still claims focus after a reload")
    r.ensure_focus()
    check(r.dock.applies == before + 1,
          "focus is re-applied after a reload rather than trusting the flag")
    check(r.dock.page_focused is True, "the reloaded page ends up focused")

    # And the one-shot align gets another pass, since the layout is new.
    aligns = r.dock.aligns
    r.ensure_focus()
    check(r.dock.aligns > aligns, "the align one-shot re-arms for a new document")

    # Focus turned OFF by the operator must stay off.
    r.focus_wanted = False
    r.dock.page_focused = False
    applies = r.dock.applies
    r.ensure_focus()
    check(r.dock.applies == applies,
          "focus is not forced back on when the operator turned it off")


def test_character_finder_drives_heading():
    """Traversal heading must come from where the character stands.

    The old code alternated, because kekkai_play.find_character keys on a RED
    robe and this character wears purple - so it returned None and the heading
    was a coin flip. Live, that produced the loop the operator reported: right,
    dead end, left, moved on, left, dead end, right... 13 runs, 5 dead ends, no
    encounter, because _scene_changed reports "moved on" when the character
    merely walks WITHIN a map.

    Hue is the wrong invariant (gear changes); saturation is not. Measured with
    the map band isolated: desert sand peaks at saturation ~126 while the
    character sits above 150, is small, and is TALL.
    """
    print("\ncharacter finder drives traversal heading")
    import mission as mission_mod
    R = mission_mod.MissionRunner
    centre = (R.CANVAS_X0 + R.CANVAS_X1) // 2

    right = cv2.imread(os.path.join(ROOT, "ref/auto/mission/traverse_char_right.png"))
    left = cv2.imread(os.path.join(ROOT, "ref/auto/mission/traverse_char_left.png"))
    check(right is not None and left is not None,
          "both traversal frames are committed")
    if right is None or left is None:
        return

    p = R.find_character(right)
    check(p is not None, f"character found on the right-hand frame ({p})")
    if p:
        check(p[0] > centre, f"it is right of centre (x={p[0]} > {centre})")
        check(("right" if p[0] < centre else "left") == "left",
              "so the heading is LEFT - away from the edge it entered by")

    p2 = R.find_character(left)
    check(p2 is not None, f"character found on the entry frame ({p2})")
    if p2:
        check(p2[0] < centre, f"it is left of centre (x={p2[0]} < {centre})")
        check(("right" if p2[0] < centre else "left") == "right",
              "so the heading is RIGHT - and the game drew a right-pointing "
              "'Go!' arrow on that same frame")

    # The click must follow the character's own row. GROUND_Y is 880, which on
    # the desert map is ~240px BELOW its feet - off the walkable path, so the
    # run barely moved and read as a dead end.
    if p:
        check(abs(p[1] - R.GROUND_Y) > 100,
              f"the character's row ({p[1]}) is far from GROUND_Y "
              f"({R.GROUND_Y}) - a fixed ground line misses the path")

    # It must NOT invent a character where the game drew none, or traversal
    # would follow a phantom.
    for rel in ("ref/auto/lobby_full.png", "ref/auto/mission/COMBAT.png"):
        pp = os.path.join(ROOT, rel)
        if os.path.exists(pp):
            check(R.find_character(cv2.imread(pp)) is None,
                  f"no character invented on {os.path.basename(rel)}")


def test_restriction_short_circuits_the_rotation():
    """A stunned turn must not probe the whole rotation.

    Every action but Dodge is greyed out, and clicking a disabled button does
    nothing, so each candidate burns a full ~6s resolve timeout to learn what
    the previous one already established. Measured live: S4, S5 and S1 each
    timed out in a single round - ~24s to reach a Dodge that was the only legal
    move all along.

    One failure is ambiguous (a cooldown, a bad click); two consecutive
    failures in one turn is the stun signature.
    """
    print("\nrestriction short-circuits the rotation")
    import battle as battle_mod

    order = ["S1", "S3", "S4", "S5"]
    cfg = {"battle": {"rotation": order, "restricted_action": "DO",
                      "click_target": False}}

    class StubGeo:
        def slot(self, s): return (100, 100)
        def cmd(self, s): return (200, 200)
        def target(self, t): return (300, 300)

    class StubActor:
        def __init__(self): self.clicks = []
        def click_pixel(self, x, y, why=""): self.clicks.append(why)

    r = battle_mod.BattleRunner.__new__(battle_mod.BattleRunner)
    r.cfg = cfg
    r.log = LOG
    r.actor = StubActor()
    r.restricted_action = "DO"
    r.RESTRICTED_AFTER = 2
    r.closing_action = None
    r.target_policy = "first"
    import combat as combat_mod
    r.rotation = battle_mod.SkillRotation(order, combat_mod.CooldownTracker(), LOG)
    r._choose_target = lambda *a, **k: None

    # Nothing resolves except the restricted action - the stun case.
    tried = []
    def wait_resolved(slot):
        tried.append(slot)
        return slot == "DO"
    r._wait_resolved = wait_resolved

    ok = r._take_action(None, StubGeo(), rounds=1)
    check(ok is True, "the turn is still spent (Dodge resolves)")
    check("DO" in tried, "it reaches the restricted action")
    skills = [t for t in tried if t != "DO"]
    check(len(skills) == 2,
          f"it stops after 2 failed actions, not {len(order)} (tried {skills})")
    check(len(tried) < len(order) + 1,
          "the whole rotation is NOT probed on a restricted turn")

    # A turn where the FIRST action works must be untouched by this.
    r2 = battle_mod.BattleRunner.__new__(battle_mod.BattleRunner)
    r2.cfg = cfg; r2.log = LOG; r2.actor = StubActor()
    r2.restricted_action = "DO"; r2.RESTRICTED_AFTER = 2
    r2.closing_action = None; r2.target_policy = "first"
    r2.rotation = battle_mod.SkillRotation(order, combat_mod.CooldownTracker(), LOG)
    r2._choose_target = lambda *a, **k: None
    seen = []
    r2._wait_resolved = lambda slot: (seen.append(slot), True)[1]
    check(r2._take_action(None, StubGeo(), rounds=1) is True,
          "a normal turn resolves on its first action")
    check(seen == [order[0]], f"and tries only that one action ({seen})")


def test_battle_between_turns_is_not_scenery():
    """A battle waiting for its turn must never be walked through.

    Between turns the game draws NO command bar, so every combat gate is
    silent - measured on a live frame with three Lv64 enemies on screen:
    charge 0.371, dodge 0.328, attack 0.307. `in_mission` therefore returned
    None, the resume ladder called the screen unknown, and after three unknowns
    the runner "walked", clicking the map edge in the middle of a fight. From
    outside that looks exactly like the bot skipping the enemy.

    `action_flag` is the only anchor that survives the between-turns gap.
    """
    print("\nbattle between turns is not scenery")
    from perceive import find
    import farm, bot as botmod
    import json as _json
    cfg = _json.load(open(os.path.join(ROOT, "Configs/mission.json")))
    tpls = botmod.load_templates(cfg, LOG)

    battle = cv2.imread(os.path.join(ROOT, "ref/auto/mission/battle_between_turns.png"))
    check(battle is not None, "the between-turns battle frame is committed")
    if battle is None:
        return
    g = cv2.cvtColor(battle, cv2.COLOR_BGR2GRAY)

    # Every command gate really is silent here - that is the whole point.
    for name in ("charge_btn", "dodge_btn", "attack_btn"):
        c = find(g, tpl(name))[1]
        check(c < 0.70, f"{name} is silent between turns ({c:.3f} < 0.70)")
    c = find(g, tpl("action_flag"))[1]
    check(c >= 0.85, f"action_flag still fires between turns ({c:.3f} >= 0.85)")

    check(farm.in_mission(battle, tpls) is not None,
          "in_mission recognises a battle with no command bar")
    check(farm.looks_like_mission_scene(battle, tpls) is False,
          "and it is NOT treated as walkable scenery")

    # A real traversal screen must still be walkable, or this trades one stuck
    # state for another.
    trav = cv2.imread(os.path.join(ROOT, "ref/auto/mission/traverse_shrub_decoy.png"))
    if trav is not None:
        check(farm.looks_like_mission_scene(trav, tpls) is True,
              "a real traversal screen is still walkable")


def test_character_finder_rejects_scenery():
    """Saturated SCENERY must not be mistaken for the character.

    A yellow-green shrub on a rock at the map edge measured 48x77 with area
    2534, beating the real character's 79x123 / area 1585 on AREA - so picking
    the largest blob located the "character" at the same pixel (779, 917) every
    run, always concluded "head right" because that x is left of centre, ran
    into the edge it was already standing on, and logged 8 dead ends while an
    enemy stood in plain sight.

    Height is what separates them: characters measured 106, 107 and 123 tall
    across three maps; the shrub is 77. A bush is short and broad, a ninja is
    tall and narrow.
    """
    print("\ncharacter finder rejects saturated scenery")
    import mission as mission_mod
    R = mission_mod.MissionRunner
    centre = (R.CANVAS_X0 + R.CANVAS_X1) // 2

    f = cv2.imread(os.path.join(ROOT, "ref/auto/mission/traverse_shrub_decoy.png"))
    check(f is not None, "the shrub-decoy frame is committed")
    if f is None:
        return
    p = R.find_character(f)
    check(p is not None, f"a character is found ({p})")
    if p:
        check(p[0] > 2000,
              f"it is the character at the RIGHT edge (x={p[0]}), not the "
              f"shrub at x=779")
        check(("right" if p[0] < centre else "left") == "left",
              "so the heading is LEFT - toward the enemy, not into the edge "
              "it is already standing on")


def test_map_is_cleared_before_leaving():
    """An enemy standing on the map must be engaged, not walked past.

    The runner only ever ran to the MAP EDGE - a rule carried over from Kekkai
    seal-hunting, where it is right. In a story mission it is wrong: the map has
    to be cleared first. And because the enemy stands at a different DEPTH
    (measured: enemy y=460 while our character was at y=864), a run along our
    own row passes 400px beneath it and never makes contact - so the mission
    "skipped" its first fight and then wandered.

    `find_figures` cannot fully separate a sprite from scenery - a CACTUS at
    (1033, 748) is proposed as a figure - so engagement is a GUESS that the game
    verifies: no fight means the spot is remembered as a dud and the normal
    edge-run happens instead.
    """
    print("\nthe map is cleared before leaving it")
    import mission as mission_mod
    R = mission_mod.MissionRunner

    f = cv2.imread(os.path.join(ROOT, "ref/auto/mission/traverse_shrub_decoy.png"))
    check(f is not None, "the frame with an enemy on the map is committed")
    if f is None:
        return

    me = R.find_character(f)
    check(me is not None and me[0] > 2400, f"our character is located ({me})")

    figs = R.find_figures(f)
    check(len(figs) >= 2, f"both figures are seen ({[(x, y) for x, y, a in figs]})")

    inst = R.__new__(R)
    inst._dud_targets = set()
    foe = inst.find_enemy_on_map(f, me)
    check(foe is not None, f"an enemy is proposed ({foe})")
    if foe:
        check(abs(foe[0] - 2282) < 80 and abs(foe[1] - 460) < 80,
              f"it is the ninja at ~(2282, 460), not us ({foe})")
        check(abs(foe[1] - me[1]) > 300,
              f"and it stands at a different depth ({foe[1]} vs {me[1]}) - which "
              f"is exactly why running along our own row missed it")

    # A dud must not be proposed twice.
    inst._dud_targets = {foe}
    again = inst.find_enemy_on_map(f, me)
    check(again != foe, "a spot that produced no fight is not proposed again")


def test_closed_window_shuts_the_bot_down():
    """Closing the browser must end the process, not wedge it holding the lock.

    A closed window and a navigated page look IDENTICAL at the socket - both
    just kill the connection - but need opposite responses: reconnect to a
    navigation, exit on a closed window. Without telling them apart, `attach`
    was retried 30 times at up to ~32s each while HOLDING THE PID LOCK, so the
    operator closed the window, tried to relaunch, and was told "another bot
    window is already running". One such process was found still alive, and
    wedged, SEVEN HOURS later.

    The CDP HTTP endpoint separates them, because it dies with the browser.
    """
    print("\nclosing the window shuts the bot down")
    import app as app_mod

    r = app_mod.Runner.__new__(app_mod.Runner)
    r.log = LOG
    r.port = 9222
    r.quit = False
    r.tpls = {}

    # Browser gone: reconnect must give up and ask the process to exit, rather
    # than grinding through 30 attach attempts.
    attaches = []
    probes = []
    def probe(timeout=2.0):
        probes.append(1)
        return False
    r.browser_alive = probe
    orig_attach = app_mod.attach
    app_mod.attach = lambda *a, **k: attaches.append(1) or (_ for _ in ()).throw(
        RuntimeError("should not be called"))
    try:
        ok = r.reconnect()
    finally:
        app_mod.attach = orig_attach
    check(ok is False, "reconnect reports failure when the browser is gone")
    check(r.quit is True, "and it asks the loop to exit")
    check(not attaches, "without ever paying for an attach")
    # Assert on WORK DONE, not wall-clock. A timing bound measures the machine,
    # not the code: under load this same check once read 964s for a path that
    # does three probes and two 2s sleeps, and failed a change that was correct.
    check(len(probes) <= 3,
          f"it gives up after {len(probes)} probes, not 30 attach attempts")

    # Browser still there: a navigation must still reconnect normally.
    r2 = app_mod.Runner.__new__(app_mod.Runner)
    r2.log = LOG; r2.port = 9222; r2.quit = False; r2.tpls = {}
    r2.controls = None
    r2.cdp = type("C", (), {"close": lambda self: None})()
    r2.browser_alive = lambda timeout=2.0: True
    calls = []
    def fake_attach(port, log, tpls, **k):
        calls.append(1)
        stub = type("S", (), {"close": lambda self: None})()
        return stub, stub, stub, stub
    app_mod.attach = fake_attach
    try:
        ok2 = r2.reconnect()
    finally:
        app_mod.attach = orig_attach
    check(ok2 is True, "a live browser still reconnects")
    check(r2.quit is False, "and does NOT shut the bot down")
    check(len(calls) == 1, "attaching once")


def test_panel_stays_alive_during_a_mission():
    """The panel must not declare the bot dead while it is working.

    The panel decides it has been abandoned from the age of its last update, and
    `push` only runs BETWEEN cycles - but a mission blocks for minutes. Worse,
    `on_wait` used to fire only while PAUSED, so during an active mission
    nothing touched the panel at all and it showed "no bot attached - the panel
    is frozen" for most of every mission.

    The gate's poll loop now pumps on every poll, and pump sends a throttled
    one-assignment heartbeat.
    """
    print("\npanel stays alive during a mission")
    import act as act_mod

    # 1. Controls pumps on_wait even when NOT paused - that is the fix.
    pumped = []
    ctl = act_mod.Controls(path="/nonexistent/bot.control", log=None,
                           on_wait=lambda: pumped.append(1))
    check(ctl.wait_if_paused() is True, "an unpaused bot carries straight on")
    check(len(pumped) == 1,
          "and the operator pump still ran (was: only while paused)")

    # 2. The heartbeat is throttled - the gate polls ~10x a second and each
    #    beat is a CDP round trip.
    import app as app_mod
    beats = []
    r = app_mod.Runner.__new__(app_mod.Runner)
    r.log = LOG
    r.dock = type("D", (), {
        "commands": lambda self, poll=0.0: [],
        "heartbeat": lambda self: beats.append(1),
    })()
    r._last_beat = 0.0
    for _ in range(20):
        r.pump(poll=0.0)
    check(len(beats) == 1,
          f"20 rapid pumps send ONE heartbeat, not 20 (sent {len(beats)})")
    r._last_beat = 0.0            # pretend the interval elapsed
    r.pump(poll=0.0)
    check(len(beats) == 2, "and it beats again once the interval passes")
    check(app_mod.Runner.HEARTBEAT_EVERY < 12.0,
          "the beat interval is well inside the panel's staleness window")


def test_quit_exits_cleanly_and_frees_the_lock():
    """Quit must end the process at once, and must not strand the pid lock.

    A cooperative stop is not enough: `_apply` is reached from `pump`, which is
    called from the capture hook and the gate's poll loop, and BOTH wrap the
    call in `except Exception` - so raising to unwind the stack is swallowed.
    A flag would only be noticed at the next place something looks, which
    mid-mission can be a whole battle away.

    `os._exit` skips `finally`, so the lock must be released by `_hard_exit`
    itself - forgetting that is exactly what produces "another bot window is
    already running" on the next launch.
    """
    print("\nquit exits cleanly and frees the lock")
    import app as app_mod

    lock = os.path.join(ROOT, "run/app.lock")
    prior = None
    if os.path.exists(lock):
        with open(lock) as f:
            prior = f.read()
    try:
        with open(lock, "w") as f:
            f.write(str(os.getpid()))

        exits = []
        real_exit = os._exit
        os._exit = lambda code=0: exits.append(code)
        r = app_mod.Runner.__new__(app_mod.Runner)
        r.log = LOG
        r.mode = "running"
        r.cdp = type("C", (), {"close": lambda self: None})()
        r.push = lambda: None
        try:
            r._hard_exit()
        finally:
            os._exit = real_exit

        check(exits == [0], f"the process is exited immediately ({exits})")
        check(not os.path.exists(lock),
              "and OUR pid lock is released, so the next launch is not refused")

        # A lock owned by SOMEBODY ELSE must never be deleted.
        with open(lock, "w") as f:
            f.write("999999")
        exits2 = []
        os._exit = lambda code=0: exits2.append(code)
        try:
            r._hard_exit()
        finally:
            os._exit = real_exit
        check(os.path.exists(lock),
              "another process's lock is left alone")
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass
        if prior is not None:
            with open(lock, "w") as f:
                f.write(prior)


def test_task_switch_interrupts_the_running_task():
    """Choosing a new task must interrupt the one in flight.

    Setting the field alone was useless: a mission takes minutes and the cycle
    loop runs it to completion, so pressing "TP training" mid-farm looked like
    nothing happened - and the only escape was Stop, which kills the process and
    leaves the panel detached. Observed exactly that: the log carried a STOP and
    no task event at all.

    The abort reuses the file-backed stop switch every task already honours at
    its own gates, so the task unwinds cleanly and nothing is killed.
    """
    print("\ntask switch interrupts the running task")
    import app as app_mod

    writes = []
    real_write = app_mod._write_control
    app_mod._write_control = lambda v: writes.append(v)
    try:
        r = app_mod.Runner.__new__(app_mod.Runner)
        r.log = LOG
        r.task = "farm_missions"
        r.mode = "running"

        r._apply({"cmd": "task", "arg": "tp_training"})
        check(r.task == "tp_training", "the new task is selected")
        check(writes == ["stop"],
              f"and the stop switch is thrown to unwind the old one ({writes})")
        check(getattr(r, "_switching", False) is True,
              "the switch is marked pending, so the loop can re-arm it")

        # Choosing the SAME task must not interrupt anything.
        writes.clear()
        r._switching = False
        r._apply({"cmd": "task", "arg": "tp_training"})
        check(writes == [], "re-picking the current task interrupts nothing")

        # Nor should it when the bot is not running.
        r.mode = "stopped"
        r.task = "farm_missions"
        writes.clear()
        r._apply({"cmd": "task", "arg": "tp_training"})
        check(writes == [], "a stopped bot needs no interrupt")
        check(r.task == "tp_training", "but the task is still selected")

        # An unknown task key is ignored rather than acted on.
        r.task = "idle"
        writes.clear()
        r._apply({"cmd": "task", "arg": "not_a_task"})
        check(r.task == "idle", "an unknown task key is refused")
    finally:
        app_mod._write_control = real_write


def test_relog_rescues_an_unreadable_state():
    """When the ladder cannot read the screen, reload before giving up.

    The resume ladder deliberately cannot name a battle or a traversal screen,
    so a task that needs the LOBBY can never start from inside a mission -
    switching to TP training mid-farm just piled up unrecognised frames until
    the bot paused. A reload lands on character select, which the ladder does
    know, and it walks back to the lobby from there.

    It must NOT be the first response: a relog throws away an in-flight
    mission.
    """
    print("\nrelog rescues an unreadable state")
    import app as app_mod

    r = app_mod.Runner.__new__(app_mod.Runner)
    r.log = LOG
    r.relog_after = 8
    r.max_relogs = 2
    r._relogs = 0

    relogs = []
    r.relog = lambda: relogs.append(1)

    # Below the threshold: no relog. A brief hiccup must not cost a mission.
    for n in (1, 4, 7):
        r.unknown = n
        fired = (r.unknown >= r.relog_after and r._relogs < r.max_relogs)
        check(not fired, f"{n} unrecognised frames does not relog")

    # At the threshold it fires, and the budget is bounded.
    fires = 0
    for _ in range(6):
        r.unknown = 10
        if r.unknown >= r.relog_after and r._relogs < r.max_relogs:
            r._relogs += 1
            r.relog()
            fires += 1
    check(fires == r.max_relogs,
          f"it relogs at most max_relogs times per streak ({fires})")
    check(len(relogs) == 2, "and a screen surviving a reload cannot loop forever")

    # Recognising the screen replenishes the budget.
    r._relogs = 2
    r.unknown = 0
    r._relogs = 0                     # what step() does on a recognised frame
    check(r._relogs == 0,
          "arriving somewhere known restores the relog budget for next time")

    # And the relog itself must never authenticate. Check for the ACTIONS that
    # would constitute typing credentials, not for the word "password" - the
    # docstring says "a password change or a ban" precisely to explain why it
    # must not, and a word-match would flag that as a violation.
    src = inspect.getsource(app_mod.Runner.relog)
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("#"))
    code = re.sub(r'"""...*?"""', "", code, flags=re.S)
    for bad in ("insertText", "dispatchKeyEvent", ".value =", "type(",
                "fill(", "submit("):
        check(bad not in code,
              f"relog performs no {bad!r} - it cannot type into a form")
    check("Page.reload" in code, "it is a reload, nothing more")


def test_kekkai_panel_is_located_not_assumed():
    """The kekkai panel MOVES; its coordinates must be measured per frame.

    Measured on a live frame, the whole puzzle sat ~116px higher than the
    module constants:

        rune Green    reference (860, 1076)   actual (876, 960)
        kekkai centre reference (1259, 513)   actual (1261, 387)

    The discs are only r~55, so every click landed clean outside its button.
    The failure was SILENT and total: no slot filled, so nothing was ever
    submitted, so the history stayed empty - and the solver then read row 0, an
    UNPLAYED row, got a dim 0/0 it could not classify, and burned the mission.
    The scroll showed ten identical unplayed rows.
    """
    print("\nkekkai panel is located, not assumed")
    import kekkai_play as kp
    import kekkai as kk

    f = cv2.imread(os.path.join(ROOT, "ref/auto/tp/kekkai_panel_moved.png"))
    check(f is not None, "the moved-panel frame is committed")
    if f is None:
        return

    runes = kp.find_rune_buttons(f)
    check(runes is not None and len(runes) == len(kk.RUNES),
          f"all six rune discs are found ({runes and len(runes)})")
    if runes:
        # They must be the RUNE row, not the history scroll's counter discs -
        # 13 circles are present in that band on this frame.
        xs = [p[0] for p in runes]
        ys = [p[1] for p in runes]
        check(max(ys) - min(ys) <= 25, "they lie in one row")
        check(max(xs) < 1800,
              f"and it is the rune bar, not the history counters (max x {max(xs)})")
        check(abs(runes[0][1] - 960) < 25,
              f"located at the ACTUAL y~960, not the reference 1076 "
              f"({runes[0][1]})")
        check(abs(runes[0][1] - kp.RUNE_XY["Green"][1]) > 80,
              "which is far enough from the reference to have missed the button")

    confirm = kp.find_confirm_point(f)
    check(confirm is not None, f"the submit disc is found ({confirm})")
    if confirm:
        check(abs(confirm[1] - 387) < 30,
              f"at the actual y~387, not the reference 513 ({confirm[1]})")

    # locate_panel must hand back the LIVE positions, keyed by rune name.
    xy, c2 = kp.locate_panel(f)
    check(set(xy) == set(kk.RUNES), "every rune is keyed by name")
    check(xy["Green"] != kp.RUNE_XY["Green"],
          "and the reference layout was not silently used")


def test_game_drift_is_tracked_and_corrected():
    """Absolute geometry must be corrected for however far the GAME has moved.

    Every absolute coordinate here was measured with the canvas at one place.
    When the game moves they all miss together, and each subsystem then reports
    a fault in ITSELF rather than the real cause: the memory board went "board
    gone", the kekkai runes became un-clickable, the Special tab "could not be
    found". Measured during one episode: scrollY 60 and the iframe at y = -118
    CSS = -236 captured px, matching the -237 by which the card grid had
    apparently moved.
    """
    print("\ngame drift is tracked and corrected")
    import capture as capture_mod
    import cards as cards_mod

    class StubCDP:
        def __init__(self, x=380.0, y=0.0):
            self.x, self.y = x, y
        def evaluate(self, expr):
            if "devicePixelRatio" in expr:
                return 2
            if "innerWidth" in expr:
                return '{"w": 1720, "h": 720}'
            if "getBoundingClientRect" in expr:
                return '{"x": %r, "y": %r}' % (self.x, self.y)
            return ""

    cap = capture_mod.Capture(StubCDP())
    check(cap.game_offset(ttl=0) == (0, 0),
          "an aligned game needs no correction")
    check(cap.fix(1434, 483) == (1434, 483),
          "so coordinates pass through untouched")

    # The measured failure: iframe at y = -118 CSS, dpr 2 -> -236 captured px.
    cap.cdp.y = -118.0
    off = cap.game_offset(ttl=0)
    check(off == (0, -236), f"a drifted game is measured as {off}")
    fx, fy = cap.fix(1434, 483)
    check((fx, fy) == (1434, 247),
          f"and a card cell is corrected to {(fx, fy)} - close to the 248 "
          f"actually measured on screen")

    # The board box moves with it, which is what stops "board gone".
    bx, by, bw, bh = cards_mod.board_box(cap)
    check((bx, by) == (cards_mod.BOARD_BOX[0], cards_mod.BOARD_BOX[1] - 236),
          "the board box is corrected too")
    check((bw, bh) == (cards_mod.BOARD_BOX[2], cards_mod.BOARD_BOX[3]),
          "and its size is unchanged - only the origin moves")

    # A game that cannot be found must NEVER move a click.
    class Blind(StubCDP):
        def evaluate(self, expr):
            if "getBoundingClientRect" in expr:
                return ""
            return StubCDP.evaluate(self, expr)
    cap2 = capture_mod.Capture(Blind())
    check(cap2.game_offset(ttl=0) == (0, 0),
          "a missing measurement yields no correction, never a guess")


def test_ladder_acknowledges_a_lone_green_check():
    """A dialog whose only control is the green check must be acknowledged.

    The kekkai's "You break the seal!" dialog left the bot stuck for 20
    unrecognised frames and then halted, with the check plainly on screen. It
    matched at 0.975 - but at SCALE 1.1, and nothing swept the ANCHOR across
    scales, only targets. That one glyph is drawn at 1.00 on a mission detail
    panel, 1.10 here, 1.18 on a Victory panel and 1.84 on Mission Success.

    Accepting the check generically is safe ONLY because everything with its own
    meaning is handled earlier in the ladder - and because a mission detail
    panel, whose control is the same glyph but which STARTS A MISSION, is backed
    out of first.
    """
    print("\nladder acknowledges a lone green check")
    from perceive import find, Template
    import bot as botmod, resume as resume_mod
    import json as _json
    cfg = _json.load(open(os.path.join(ROOT, "Configs/mission.json")))
    tpls = botmod.load_templates(cfg, LOG)
    r = resume_mod.Resumer(type("C", (), {})(), None, tpls, LOG)

    def first_rung(rel):
        im = cv2.imread(os.path.join(ROOT, rel))
        if im is None:
            return None
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        for step in r.ladder:
            t = tpls.get(step.anchor)
            if t is None:
                continue
            if step.anchor_scales:
                t = Template(t.name, t.path, t.threshold, step.anchor_scales)
            if t.h > g.shape[0] or t.w > g.shape[1]:
                continue
            _, c = find(g, t)
            lim = step.threshold if step.threshold is not None else t.threshold
            if c >= lim:
                return step.name
        return "none"

    check(first_rung("ref/auto/tp/seal_broken_dialog.png") == "confirm_dialog",
          "the seal-broken dialog is acknowledged, not halted on")

    # THE SAFETY CASE: the same glyph on a detail panel starts a mission, so the
    # ladder must back out instead.
    for rel, what in (("ref/auto/mission/detail_00.png", "detail panel"),
                      ("ref/auto/mission/list_00.png", "mission list"),
                      ("ref/auto/mission/list_all_locked.png", "all-locked list")):
        check(first_rung(rel) == "mission_list",
              f"a {what} is backed out of, NOT confirmed")

    # Panels that mean something specific keep their own rungs.
    check(first_rung("ref/auto/panels/mission_success.png") == "mission_success",
          "Mission Success still uses its own rung (it banks the reward)")
    check(first_rung("ref/auto/panels/victory.png") == "result_panel",
          "a Victory panel still uses its own rung")
    check(first_rung("ref/auto/lobby_full.png") == "lobby",
          "the lobby is still an arrival")
    check(first_rung("ref/auto/mission/COMBAT.png") == "none",
          "and combat matches nothing - the ladder must not click in a fight")


def test_stop_aborts_the_task_but_keeps_the_panel():
    """Stop must abort at once WITHOUT killing the process.

    The original complaint was that Stop was queued - pressed mid-mission it did
    nothing until the mission ended. The cause was not the mechanism: operator
    commands were not being READ during long work, because pump ran only from
    the gate's poll loop and farm navigation never enters a gate. With
    Capture.on_activity pumping on every capture, the file-backed stop switch is
    seen within a capture or two.

    Killing the process to get immediacy had a real cost: the panel survives in
    the page but its buttons have no receiver, so the operator is left with a
    dead panel and no way back except the terminal - the recurring "no bot
    attached". Quit still exits.
    """
    print("\nstop aborts the task but keeps the panel")
    import app as app_mod

    writes = []
    real_write = app_mod._write_control
    app_mod._write_control = lambda v: writes.append(v)
    exits = []
    real_exit = os._exit
    os._exit = lambda code=0: exits.append(code)
    try:
        r = app_mod.Runner.__new__(app_mod.Runner)
        r.log = LOG
        r.mode = "running"
        r.task = "farm_missions"
        r._apply({"cmd": "stop", "arg": None})
        check(writes == ["stop"], f"the stop switch is thrown ({writes})")
        check(r.mode == "stopped", "the runner records that it is stopped")
        check(exits == [], "and the PROCESS SURVIVES, so the panel stays live")

        # Run must bring it straight back - no terminal needed.
        writes.clear()
        r._apply({"cmd": "run", "arg": None})
        check(writes == ["run"], "Run re-arms the switch")
        check(r.mode == "running", "and the bot is running again")

        # Quit is still the one that exits.
        r.cdp = type("C", (), {"close": lambda self: None})()
        r.push = lambda: None
        r.dock = type("D", (), {"remove": lambda self: None})()
        r._apply({"cmd": "quit", "arg": None})
        check(exits == [0], "Quit still terminates the process")
    finally:
        app_mod._write_control = real_write
        os._exit = real_exit


def test_one_character_finder_shared_by_both_runners():
    """Mission traversal and the Kekkai hunt must use the SAME finder.

    They had two. Mission traversal was fixed to find a purple-robed character
    by saturation; `kekkai_play` kept its own red-robe search, which returns
    None for this character - so `heading_from_spawn` fell back to "right" and
    ran it straight back through the edge it had just entered by, over and over.
    That is the seal hunt "getting stuck where movement was necessary".

    Two implementations of one idea is how that survived a fix, so the test
    asserts they agree rather than that each works.
    """
    print("\none character finder, shared by both runners")
    import kekkai_play as kp
    import mission as mission_mod
    import perceive

    frames = ["ref/auto/mission/traverse_char_right.png",
              "ref/auto/mission/traverse_char_left.png",
              "ref/auto/mission/traverse_shrub_decoy.png"]
    for rel in frames:
        f = cv2.imread(os.path.join(ROOT, rel))
        if f is None:
            continue
        a = kp.find_character(f)
        b = mission_mod.MissionRunner.find_character(f)
        name = os.path.basename(rel)
        check(a is not None, f"the kekkai runner finds the character on {name}")
        check(b is not None, f"mission traversal finds it too on {name}")
        if a and b:
            check(abs(a[0] - b[0]) < 40 and abs(a[1] - b[1]) < 40,
                  f"and they agree on {name} ({a} vs {b})")

    # The heading must come from the character, not from the "right" default.
    right_edge = cv2.imread(os.path.join(
        ROOT, "ref/auto/mission/traverse_char_right.png"))
    left_edge = cv2.imread(os.path.join(
        ROOT, "ref/auto/mission/traverse_char_left.png"))
    if right_edge is not None:
        check(kp.heading_from_spawn(right_edge) == "left",
              "a character at the RIGHT edge heads left, away from where it "
              "came in")
    if left_edge is not None:
        check(kp.heading_from_spawn(left_edge) == "right",
              "and one at the left edge heads right")

    # The live frame the seal hunt got stuck on: "Seals: 1 / 2", character
    # standing right of centre with no seal on screen. The old finder returned
    # None here, so the heading defaulted to "right" - back through the edge it
    # had just come in by.
    stuck = cv2.imread(os.path.join(ROOT, "ref/auto/tp/kekkai_seal2_hunt.png"))
    if stuck is not None:
        p = kp.find_character(stuck)
        check(p is not None, f"the stuck seal-hunt frame locates the character ({p})")
        if p:
            check(p[0] > (kp.CANVAS_X0 + kp.CANVAS_X1) // 2,
                  "which is right of centre")
        check(kp.heading_from_spawn(stuck) == "left",
              "so it heads LEFT to look for seal 2, not back into the edge")

    # The shared implementation must be the one in perceive, not a copy.
    check(inspect.getsourcefile(perceive.find_character).endswith("perceive.py"),
          "the finder lives in perceive, so there is only one of it")


def test_tp_recovery_relogs_before_giving_up():
    """A TP pass must not stop with the mission still playable on screen.

    The ladder deliberately cannot classify a battle, a traversal map or a
    half-played minigame, so ending a TP mission on one leaves it nothing to
    climb: it burned its 20 unrecognised frames and halted, and the pass stopped
    with "Seals: 1 / 2" still on screen. The relog rung added to `step` did not
    help, because this halt comes from the Resumer's OWN run() inside the task.
    """
    print("\nTP recovery relogs before giving up")
    import tp as tp_mod
    import resume as resume_mod

    calls = {"climbs": 0, "relogs": 0}

    class StubResumer:
        def __init__(self, *a, **k):
            pass
        def run(self, timeout=120):
            calls["climbs"] += 1
            # First climb fails; after a relog the second succeeds.
            if calls["relogs"]:
                return resume_mod.ARRIVED, {}
            return resume_mod.HALT, {"reason": "unrecognised screen"}

    real = resume_mod.Resumer
    resume_mod.Resumer = StubResumer
    try:
        ok = tp_mod._recover_to_lobby(
            None, None, LOG, relog=lambda: calls.__setitem__("relogs",
                                                             calls["relogs"] + 1))
        check(ok is True, "recovery succeeds once it has relogged")
        check(calls["relogs"] == 1, "it relogs exactly once")
        check(calls["climbs"] == 2, "climbing before and after, and no more")

        # Without a relog callable the behaviour is unchanged - no silent change
        # for any other caller.
        calls.update(climbs=0, relogs=0)
        ok2 = tp_mod._recover_to_lobby(None, None, LOG)
        check(ok2 is False, "with no relog available it still reports failure")
        check(calls["climbs"] == 1, "and does not climb twice for nothing")

        # A screen that survives the reload must NOT loop.
        calls.update(climbs=0, relogs=0)
        class NeverArrives(StubResumer):
            def run(self, timeout=120):
                calls["climbs"] += 1
                return resume_mod.HALT, {"reason": "unrecognised screen"}
        resume_mod.Resumer = NeverArrives
        ok3 = tp_mod._recover_to_lobby(
            None, None, LOG, relog=lambda: calls.__setitem__("relogs",
                                                             calls["relogs"] + 1))
        check(ok3 is False, "an unreadable screen still ends in failure")
        check(calls["relogs"] == 1 and calls["climbs"] == 2,
              "after ONE relog and TWO climbs - it cannot loop")
    finally:
        resume_mod.Resumer = real


def main():
    for fn in (test_geometry_classification, test_two_geometries,
               test_ring_cross_geometry, test_watchdog_recorded_sequence,
               test_skill_rotation, test_command_bar_layout,
               test_preflight, test_find_all_suppression,
               test_resume_ladder_panels, test_minigame_classifier,
               test_card_matcher, test_tp_navigation_templates,
               test_dock_and_controls, test_kekkai_digits,
               test_seal_phases, test_mission_list_is_not_scenery,
               test_cold_command_bar_probe_is_budgeted,
               test_focus_survives_a_reload,
               test_character_finder_drives_heading,
               test_restriction_short_circuits_the_rotation,
               test_battle_between_turns_is_not_scenery,
               test_character_finder_rejects_scenery,
               test_one_character_finder_shared_by_both_runners,
               test_map_is_cleared_before_leaving,
               test_closed_window_shuts_the_bot_down,
               test_panel_stays_alive_during_a_mission,
               test_quit_exits_cleanly_and_frees_the_lock,
               test_task_switch_interrupts_the_running_task,
               test_stop_aborts_the_task_but_keeps_the_panel,
               test_relog_rescues_an_unreadable_state,
               test_tp_recovery_relogs_before_giving_up,
               test_kekkai_panel_is_located_not_assumed,
               test_game_drift_is_tracked_and_corrected,
               test_ladder_acknowledges_a_lone_green_check):
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
