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
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))

import combat                                             # noqa: E402
from battle import SkillRotation                          # noqa: E402
from geometry import BattleGeometry, COMMAND, CMD_SIDE     # noqa: E402
from mission import REQUIRED_TEMPLATES, preflight, _find_all  # noqa: E402
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
        # The ladder covers login->lobby and result panels. A mission room or a
        # battle is NOT its job, and it must say "unknown" and click nothing
        # rather than guess - clicking blind is how you hit Delete next to Play.
        ("mission room", "ref/auto/mission/room_05.png", "unknown", False),
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
    check("scroll" in tprun.SUPPORTED and "potion" not in tprun.SUPPORTED,
          "scroll is supported, potion (the hand-seal game) is still refused")


def main():
    for fn in (test_geometry_classification, test_two_geometries,
               test_ring_cross_geometry, test_watchdog_recorded_sequence,
               test_skill_rotation, test_command_bar_layout,
               test_preflight, test_find_all_suppression,
               test_resume_ladder_panels, test_minigame_classifier,
               test_card_matcher, test_tp_navigation_templates):
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
