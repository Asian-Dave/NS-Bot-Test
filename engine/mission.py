"""Mission runner — pick a mission, play it, bank the reward.

This is the reference bot's `FormMission` (3,862 lines) translated to our stack.
Almost none of its structure survives the trip, and that is the point: it can
afford a linear script because it pins one window geometry and one server. We
cannot, for a reason CLAUDE.md states outright —

    "Mission flow varies between missions. #1: cutscene -> traversal -> combat.
     #2: cutscene -> loading -> combat (no traversal), then traversal later.
     Branch on observed state; never follow a fixed script."

So this is a DISPATCH LOOP, not a sequence. Each pass captures a frame, decides
what state we are in, does the one thing that state calls for, and loops. The
mission's shape is discovered as it happens. That also means an unexpected
interruption — a popup, a stray loading screen, a disconnect — is handled by the
same machinery as everything else, instead of derailing a script.

THE FLOW WE EXPECT (from CLAUDE.md, measured by hand)
-----------------------------------------------------
    Mission Room -> grade (S locked / A / B / C)
      -> paginated list (3 per page; Grade A = 7 pages, Grade C = 11)
      -> detail panel [Completed: N] -> green check
      -> cutscene ("click anywhere to continue")
      -> traversal (click to walk; encounters trigger on movement)
      -> N battles (Victory! panel each; XP 0 / Gold 0 mid-mission is NORMAL)
      -> epilogue cutscene
      -> "Mission Success!"  <- the real rewards land HERE

Two facts from that list are load-bearing and easy to get wrong:

  * Mid-mission Victory panels show XP 0 / Gold 0. That is not a failure and not
    a sign of a wasted fight. Only "Mission Success!" carries real rewards, so
    only that state may increment a success counter.
  * Battle count is NOT the node count on the traversal track. "The Criminal
    Gathering" (Lv 56, Grade A) took SEVEN battles while showing three nodes. So
    the loop must not stop after a predicted number of fights; it stops when the
    game says the mission is over.

GRADE CHOICE IS THE WHOLE GAME ECONOMICALLY
-------------------------------------------
Grade C page 1 gives 20 XP. Grade A page 1 gives 4,870. Anything that farms
Grade C by default is wasting the run, so `grade` is required config with no
default rather than something we guess.

LOCKED MISSIONS
---------------
Grade A spans Lv 42-78 in +2 steps. Missions above the character's level render
greyed with a padlock and are INERT — clicking one does nothing, forever. A
locked-row detector is mandatory or the bot dead-loops on a dead row. That is
`skip_locked`, and it is on by default.
"""
import time

import cv2

import battle as battle_mod
from gate import Stopped, template as cond_template
from geometry import BattleGeometry


# --- what this module needs that tpl/ does not yet contain ------------------
# Each entry: template name -> (what it must show, how to cut it).
# The runner refuses to start live until these exist, and says exactly which are
# missing. Better a loud preflight failure than a bot clicking hopefully at a
# screen it cannot read.
REQUIRED_TEMPLATES = {
    "mission_room": (
        "A unique, opaque element of the Mission Room shell.",
        "Cut from the room's own chrome — a tab border or panel corner. NOT the "
        "room title if it is drawn over animated art."),
    "grade_tab": (
        "The grade selector row (A / B / C / S).",
        "One template per grade tab is easier than one for the row; name them "
        "grade_a_tab etc. and set `grade_template` in config."),
    "mission_row": (
        "One mission row in the paginated list (3 per page).",
        "Cut the row frame, not the mission name — names differ per row."),
    "mission_locked": (
        "The padlock / greyed state on an above-level row.",
        "Cut the padlock glyph itself. This one is REQUIRED for safety: without "
        "it the bot can dead-loop clicking an inert row."),
    "page_next": (
        "The list's next-page arrow.",
        "Opaque UI arrow. Check it has a distinct disabled state, or gate paging "
        "on the row contents changing instead."),
    "mission_start": (
        "The green check that starts the mission, bottom-right of the detail panel.",
        "Opaque and high-contrast; should template well. Do NOT confuse with the "
        "back arrow bottom-left."),
    "result_panel": (
        "The mid-mission Victory! panel.",
        "Cut the panel frame or the word Victory. Remember XP 0 / Gold 0 here is "
        "normal."),
    "mission_success": (
        "The end-of-mission 'Mission Success!' panel.",
        "This is the ONLY state that means real rewards. Must be distinct from "
        "result_panel or the counters will lie."),
    "cutscene_continue": (
        "The 'click anywhere to continue' prompt.",
        "tpl/click_to_continue.png EXISTS BUT IS UNUSABLE — measured 0.642-0.849 "
        "across unrelated states and it false-fires on combat, which made every "
        "frame classify as 'cutscene'. It must be RE-CUT before use."),
}


class MissionOutcome:
    SUCCESS = "success"
    ABORTED = "aborted"
    FAILED = "failed"
    STALLED = "stalled"
    STOPPED = "stopped"
    LOCKED = "locked_only"      # every candidate row was above our level


def preflight(templates, log, required=None):
    """Which required templates are missing. Returns a list of names.

    Called before any live run. We do not attempt a mission with a hole in
    perception — a missing `mission_locked` in particular turns a locked row into
    an infinite click loop.
    """
    need = required or REQUIRED_TEMPLATES
    missing = [n for n in need if n not in templates]
    if missing:
        log.error("mission runner cannot start: %d template(s) missing",
                  len(missing))
        for n in missing:
            what, how = need[n]
            log.error("  %-18s %s", n, what)
            log.error("  %-18s   -> %s", "", how)
    return missing


class MissionRunner:
    """Play one mission, end to end, by observing and branching.

    Deliberately does NOT know the mission's internal shape. It knows a set of
    states, what to do in each, and that it is finished when it sees
    'Mission Success!' or gives up.
    """

    def __init__(self, gate, actor, capture, templates, cfg, log, controls=None):
        self.gate, self.actor, self.capture = gate, actor, capture
        self.templates, self.cfg, self.log = templates, cfg, log
        self.controls = controls

        m = cfg.get("mission", {})
        self.grade = m.get("grade")               # required; no default on purpose
        self.skip_locked = m.get("skip_locked", True)
        self.max_steps = m.get("max_steps", 400)
        self.max_battles = m.get("max_battles", 25)
        self.state_timeout = m.get("state_timeout_s", 120)
        self.traversal_click = m.get("traversal_click")   # (x, y) fraction of canvas

        self.conditions = self._build_conditions()
        self.stats = {"battles": 0, "victories": 0, "aborted": 0,
                      "cutscenes": 0, "steps": 0}

    # -- conditions ----------------------------------------------------------
    def _build_conditions(self):
        """Named gate conditions, assembled from whichever templates exist.

        Built defensively: a missing template yields no condition rather than an
        exception, so the dry-run observation path still works on a partial
        template set and reports what it could and could not see.
        """
        c = {}
        t = self.templates
        for key in ("result_panel", "mission_success", "mission_start",
                    "mission_room", "mission_locked", "page_next",
                    "cutscene_continue"):
            if key in t:
                c[key] = cond_template(key, t[key])
        if "loading_text" in t:
            c["loading"] = cond_template("loading", t["loading_text"])

        # Combat gates. Two corroborating command buttons, per CLAUDE.md's
        # discrimination matrix — never attack_btn, which only reaches 0.791.
        if "charge_btn" in t and "dodge_btn" in t:
            ch, do = t["charge_btn"], t["dodge_btn"]

            def _bar(frame_bgr, gray, _ch=ch, _do=do):
                return BattleGeometry.locate(gray, _ch, _do)

            c["command_bar"] = cond_template("command_bar", ch)
            c["command_bar"].check = _bar

            def _bar_gone(frame_bgr, gray, _ch=ch, _do=do):
                return True if BattleGeometry.locate(gray, _ch, _do) is None else None

            c["command_bar_gone"] = cond_template("command_bar_gone", ch)
            c["command_bar_gone"].check = _bar_gone
        return c

    # -- classification ------------------------------------------------------
    def classify(self, frame_bgr, frame_gray):
        """Which mission state is on screen. Priority order, never presence alone.

        Ordering rules encoded here, all from measurement:
          * mission_success BEFORE result_panel — the success panel is the one
            that matters and the two are visually similar.
          * result_panel BEFORE command_bar — the result panel draws OVER the
            command bar, so a finished fight would otherwise read as "my turn".
          * loading fairly early — a loading screen hides everything else, and
            mistaking it for "unknown" burns the step budget.
        """
        order = ("mission_success", "result_panel", "loading", "command_bar",
                 "mission_start", "cutscene_continue", "mission_room")
        for name in order:
            cond = self.conditions.get(name)
            if cond is None:
                continue
            payload = cond.check(frame_bgr, frame_gray)
            if payload:
                return name, payload
        return "unknown", None

    # -- the dispatch loop ---------------------------------------------------
    def run(self):
        """Play until Mission Success, a verdict, or the step budget runs out."""
        if not self.grade:
            raise ValueError(
                "mission.grade is required and has no default. Grade C page 1 "
                "yields 20 XP against Grade A's 4,870 — guessing this wastes the "
                "run.")

        last_state, repeats = None, 0
        while self.stats["steps"] < self.max_steps:
            if self.controls is not None and not self.controls.wait_if_paused():
                return MissionOutcome.STOPPED, self.stats
            self.stats["steps"] += 1

            bgr = self.capture.frame(gray=False)
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            state, payload = self.classify(bgr, gray)

            # A state that will not advance is the failure mode this loop is most
            # prone to — a locked mission row, or a dismissed-but-redrawn panel.
            # Count consecutive repeats and bail rather than spin forever.
            if state == last_state:
                repeats += 1
            else:
                self.log.info("mission: %s -> %s (step %d)", last_state, state,
                              self.stats["steps"])
                last_state, repeats = state, 0
            if repeats >= self.cfg.get("mission", {}).get("max_repeats", 25):
                self.log.error("mission: stuck in %r for %d steps", state, repeats)
                return MissionOutcome.STALLED, self.stats

            if state == "mission_success":
                self.log.info("mission: SUCCESS after %d battles",
                              self.stats["battles"])
                if payload is not None and getattr(payload, "center", None):
                    self.actor.click_pixel(*payload.center,
                                           why="acknowledge Mission Success")
                return MissionOutcome.SUCCESS, self.stats

            if state == "command_bar":
                out = self._fight()
                if out == battle_mod.STOPPED:
                    return MissionOutcome.STOPPED, self.stats
                if out in (battle_mod.STALLED,):
                    return MissionOutcome.STALLED, self.stats
                if out == battle_mod.DEFEAT:
                    return MissionOutcome.FAILED, self.stats
                continue

            if state == "result_panel":
                # Mid-mission victory. XP 0 / Gold 0 here is NORMAL — do not
                # treat it as a failed fight, and do not count it as a mission
                # success. Just acknowledge and carry on.
                self.stats["victories"] += 1
                if payload is not None and getattr(payload, "center", None):
                    self.actor.click_pixel(*payload.center,
                                           why="dismiss mid-mission Victory")
                continue

            if state == "loading":
                # Cold SWF load is 25-30s, warm ~8s, and the Hunting House
                # sub-app is known to hang at 3% forever. So we wait, but with a
                # ceiling, and we do not click into a loading screen.
                self.gate.wait_for_any(
                    [c for k, c in self.conditions.items() if k != "loading"],
                    self.cfg.get("timing", {}).get("loading_timeout_s", 90),
                    why="loading")
                continue

            if state == "cutscene_continue":
                self.stats["cutscenes"] += 1
                if payload is not None and getattr(payload, "center", None):
                    self.actor.click_pixel(*payload.center, why="advance cutscene")
                continue

            if state == "mission_start":
                if payload is not None and getattr(payload, "center", None):
                    self.actor.click_pixel(*payload.center,
                                           why="start mission (green check)")
                continue

            if state == "mission_room":
                if not self._pick_mission(bgr, gray):
                    return MissionOutcome.LOCKED, self.stats
                continue

            # Unknown. Most likely traversal — CLAUDE.md notes encounters trigger
            # on movement, and the traversal screen has no reliable anchor yet.
            self._traverse(bgr)

        self.log.warning("mission: step budget %d exhausted", self.max_steps)
        return MissionOutcome.STALLED, self.stats

    # -- pieces --------------------------------------------------------------
    def _fight(self):
        runner = battle_mod.BattleRunner(self.gate, self.actor, self.capture,
                                        self.templates, self.conditions,
                                        self.cfg, self.log)
        self.stats["battles"] += 1
        if self.stats["battles"] > self.max_battles:
            self.log.error("mission: exceeded max_battles=%d", self.max_battles)
            return battle_mod.STALLED
        outcome, info = runner.run()
        self.log.info("mission: battle %d -> %s %s",
                      self.stats["battles"], outcome, info)
        if outcome == battle_mod.ABORTED:
            self.stats["aborted"] += 1
        return outcome

    def _pick_mission(self, frame_bgr, frame_gray):
        """Choose a startable row on the current page. False if none are.

        Locked rows are skipped by template. This is where the padlock detector
        earns its place: Grade A spans Lv 42-78 and every row above the
        character's level is inert, so 'click the first row' dead-loops.
        """
        locked_pts = []
        if self.skip_locked and "mission_locked" in self.templates:
            # ALL locked rows, not just the best-scoring one. A page shows three
            # rows and more than one can be above our level; finding a single
            # padlock would leave the others looking startable.
            locked_pts = _find_all(frame_gray, self.templates["mission_locked"],
                                   max_hits=4)
            if locked_pts:
                self.log.info("mission: %d locked row(s) at %s",
                              len(locked_pts), locked_pts)

        if "mission_row" not in self.templates:
            self.log.error("mission: no mission_row template; cannot pick a row")
            return False

        rows = _find_all(frame_gray, self.templates["mission_row"])
        if not rows:
            self.log.info("mission: no rows on this page")
            return self._next_page(frame_gray)

        for (x, y) in rows:
            if any(abs(x - lx) < 60 and abs(y - ly) < 30 for lx, ly in locked_pts):
                self.log.info("mission: skipping locked row at (%d,%d)", x, y)
                continue
            self.actor.click_pixel(x, y, why="open mission detail")
            return True

        self.log.info("mission: every row on this page is locked; paging")
        return self._next_page(frame_gray)

    def _next_page(self, frame_gray):
        """Advance the paginated list. False when we run out of pages.

        Grade A is 7 pages, Grade C is 11, so paging is normal and bounded.
        """
        from perceive import find
        if "page_next" not in self.templates:
            self.log.error("mission: no page_next template; cannot page")
            return False
        m, _ = find(frame_gray, self.templates["page_next"])
        if not m.found:
            self.log.info("mission: no next-page control; end of list")
            return False
        self.actor.click_pixel(*m.center, why="next page of mission list")
        return True

    def _traverse(self, frame_bgr):
        """Nudge the character along the traversal track.

        Encounters trigger on MOVEMENT, so standing still stalls the mission
        without any error. The click point is configured as a fraction of the
        canvas rather than an absolute pixel, because canvas geometry varies (see
        geometry.py). With nothing configured we do nothing and say so — clicking
        a guessed point on an unrecognised screen is how a bot ends up pressing
        Delete next to Play.
        """
        if not self.traversal_click:
            self.log.info("mission: unknown state and no traversal_click "
                          "configured; observing only")
            time.sleep(1.0)
            return
        fx, fy = self.traversal_click
        h, w = frame_bgr.shape[:2]
        self.actor.click_pixel(int(w * fx), int(h * fy), why="traverse")


def _find_all(frame_gray, tpl, max_hits=8, suppress=None, scales=None,
              threshold=None):
    """Every place `tpl` matches, with non-max suppression. Scale-swept.

    `perceive.find` returns only the single best match, which is wrong for a list
    of three identical mission rows.

    The scale sweep is NOT optional decoration. matchTemplate is not scale
    invariant, and our templates are cut at one canvas geometry while the live
    canvas may be at another — measured 0.46 vs 0.545 across our own capture
    sets. Matching at native template size found ZERO copies of a button that was
    plainly on screen, which is exactly how a mission list would silently read as
    empty and the runner would page forever.

    So: sweep to find the best-scoring scale, then enumerate hits at that one
    scale. One scale, because all rows in a list are the same size — searching
    every scale for every hit costs time linearly for no gain.
    """
    scales = scales or [round(0.40 + i * 0.02, 2) for i in range(36)]   # .40-1.10
    thr = tpl.threshold if threshold is None else threshold
    fh, fw = frame_gray.shape[:2]

    best = None
    for s in scales:
        th, tw = int(tpl.h * s), int(tpl.w * s)
        if th < 6 or tw < 6 or th > fh or tw > fw:
            continue
        small = cv2.resize(tpl.gray, (tw, th),
                           interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
        res = cv2.matchTemplate(frame_gray, small, cv2.TM_CCOEFF_NORMED)
        _, mx, _, _ = cv2.minMaxLoc(res)
        if best is None or mx > best[0]:
            best = (float(mx), s, res, (tw, th))
    if best is None or best[0] < thr:
        return []

    _, _, res, (tw, th) = best
    rx = suppress or max(tw // 2, 8)
    ry = suppress or max(th // 2, 8)
    hits, work = [], res.copy()
    for _ in range(max_hits):
        _, mx, _, loc = cv2.minMaxLoc(work)
        if mx < thr:
            break
        hits.append((loc[0] + tw // 2, loc[1] + th // 2))
        x0, y0 = max(0, loc[0] - rx), max(0, loc[1] - ry)
        work[y0:loc[1] + ry, x0:loc[0] + rx] = -1.0
    return sorted(hits, key=lambda p: p[1])
