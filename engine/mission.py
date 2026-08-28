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
    "mission_room_entry": (
        "The 'Mission Room' plaque in the VILLAGE, i.e. the way in from the lobby.",
        "CLAUDE.md warns village labels are semi-transparent over animated art and "
        "unusable. MEASURED otherwise for this one: the plaque scored 1.000 on four "
        "lobby frames captured 1.5s apart with the scene animating, worst negative "
        "0.512. So the warning holds for Battle / Hunting House but NOT here - "
        "re-measure per label rather than assuming either way."),
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
        # A run to the edge plus the map transition. Measured in the Kekkai work:
        # a short walk settles quickly but a map change needs ~4.5s, and scanning
        # mid-transition reads as "nothing here".
        self.traverse_settle = m.get("traverse_settle_s", 5.0)
        # Between dialogue clicks. Long enough for the next screen to draw,
        # short enough that a five-screen cutscene is not a five-second wait.
        self.dialogue_settle = m.get("dialogue_settle_s", 0.45)
        self._heading = m.get("traverse_heading", "right")
        self.traversal_click = m.get("traversal_click")   # (x, y) fraction of canvas

        self.conditions = self._build_conditions()
        # `closed_out` records whether the Mission Success panel was actually
        # acknowledged and the lobby regained. A SUCCESS with closed_out False
        # means the reward may not be banked and the next mission cannot start.
        self.stats = {"battles": 0, "victories": 0, "aborted": 0,
                      "cutscenes": 0, "steps": 0, "closed_out": None}

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
                    "mission_room", "mission_room_entry", "mission_locked",
                    "page_next", "cutscene_continue"):
            if key in t:
                c[key] = cond_template(key, t[key])
        if "loading_text" in t:
            c["loading"] = cond_template("loading", t["loading_text"])
        # The lobby anchor. Needed to confirm a mission actually CLOSED OUT: the
        # run is not finished when the Mission Success panel appears, only once
        # its green check has been acknowledged and the game has returned here.
        if "lobby_rail_fortune" in t:
            c["lobby"] = cond_template("lobby", t["lobby_rail_fortune"])
        if "mission_room_entry" in t:
            c["mission_room_entry"] = cond_template("mission_room_entry",
                                                    t["mission_room_entry"])

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
                 "mission_start", "cutscene_continue", "mission_room", "lobby")
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
                # A mission is NOT finished when this panel appears. It is
                # finished when its green check has been acknowledged and the
                # game has returned to the lobby. Returning SUCCESS on sight was
                # wrong in two ways:
                #   1. it ignored whether the click actually landed, so a check
                #      we failed to locate still counted as a success;
                #   2. with --repeat N the next runner started while the panel
                #      was still up, re-classified mission_success, and banked
                #      another instant "success" - N missions from one panel,
                #      never once going back to the lobby to start a real one.
                # So: click, then WAIT for the panel to clear, then confirm the
                # lobby. Anything else is a stall, not a success.
                if not self._click_green_check(gray, "Mission Success"):
                    continue                    # retry; the loop bounds this

                cleared = self.gate.wait_until_gone(
                    self.conditions["mission_success"],
                    self.cfg.get("mission", {}).get("close_out_timeout_s", 45))
                if isinstance(cleared, Stopped):
                    return MissionOutcome.STOPPED, self.stats
                if not cleared:
                    self.log.error("mission: Mission Success panel did not clear "
                                   "after acknowledging it; the reward is not "
                                   "banked and the next mission cannot start")
                    return MissionOutcome.STALLED, self.stats

                if "lobby" in self.conditions:
                    back = self.gate.wait_for_any(
                        [self.conditions["lobby"]],
                        self.cfg.get("mission", {}).get("close_out_timeout_s", 45),
                        why="return to lobby after Mission Success")
                    if isinstance(back, Stopped):
                        return MissionOutcome.STOPPED, self.stats
                    if not back:
                        self.log.warning("mission: acknowledged Mission Success but "
                                         "the lobby anchor never appeared; "
                                         "reporting SUCCESS with a caveat")
                        self.stats["closed_out"] = False
                        return MissionOutcome.SUCCESS, self.stats
                self.stats["closed_out"] = True
                self.log.info("mission: SUCCESS after %d battles, closed out to "
                              "the lobby", self.stats["battles"])
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
                self._click_green_check(gray, "mid-mission Victory")
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
                self._skip_dialogue(payload)
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

            if state == "lobby":
                # The hop the runner previously could not make. Without it,
                # starting a mission from the lobby fell through to `unknown` ->
                # `_traverse`, which does nothing when traversal_click is unset:
                # the runner burned its whole step budget standing in the village.
                #
                # `lobby` is tested LAST in classify() so that mission_room wins
                # whenever both could match. Measured, they do not overlap: the
                # lobby rail reads 1.000 in the village and 0.297 behind the
                # Mission Room panel.
                cond = self.conditions.get("mission_room_entry")
                payload2 = cond.check(bgr, gray) if cond else None
                if payload2 is None:
                    self.log.error("mission: in the lobby but the Mission Room "
                                   "entrance was not located; cannot start")
                    return MissionOutcome.STALLED, self.stats
                self.actor.click_pixel(*payload2.center,
                                       why="enter Mission Room from the village")
                continue

            # Unknown. Most likely traversal — CLAUDE.md notes encounters trigger
            # on movement, and the traversal screen has no reliable anchor yet.
            self._traverse(bgr)

        self.log.warning("mission: step budget %d exhausted", self.max_steps)
        return MissionOutcome.STALLED, self.stats

    # -- pieces --------------------------------------------------------------
    _check_scales = []          # scales that have worked, most recent first

    def _click_green_check(self, frame_gray, why):
        """Dismiss a result panel by its GREEN CHECK, not by clicking the panel.

        MEASURED live: a Victory panel absorbed ELEVEN clicks at the canvas
        centre and did nothing at all. The panel body is not a hit area; the
        green check bottom-right is the only one. Clicking the template match
        centre (the banner) has the same problem — it is not the button.

        Note the green check is the SAME glyph the mission detail panel uses to
        START a mission, which is why `tpl/mission_start.png` doubles as the
        check here, and why `classify()` must test the result panels BEFORE
        mission_start. Otherwise a Victory panel reads as "start a mission".

        The check is drawn at THREE DIFFERENT SIZES, which is the trap here.
        Measured peaks of the same glyph:

            mission detail panel   scale 1.00   conf 0.975
            mid-mission Victory    scale 1.18   conf 0.974
            Mission Success        scale 1.84   conf 0.972

        All three are ~0.97 at their true scale, so this is a pure scale problem,
        not a quality one. A narrow sweep (0.90..1.15) caught Victory only at its
        edge and missed Mission Success entirely, scoring 0.693 — below any sane
        gate. The runner then refused to click, correctly, and the mission could
        never close out. So the sweep must span 0.95..1.95.
        """
        from perceive import find
        tpl = self.templates.get("mission_start")
        if tpl is None:
            self.log.error("mission: no green-check template; cannot dismiss %s", why)
            return False
        saved_scales, saved_thr = tpl.scales, tpl.threshold
        try:
            tpl.threshold = 0.85
            # TRY THE SCALES THAT HAVE ALREADY WORKED, THEN THE FULL SWEEP.
            # The full 21-scale sweep costs 1,538 ms against 62 ms at a known
            # scale, and it was being paid every time a panel was dismissed -
            # which is most of why the Victory screen felt slow. There are only
            # three sizes in this game (detail 1.00, Victory ~1.20, Success
            # 1.84), so after the first sighting of each the cache answers.
            full = [round(0.95 + i * 0.05, 2) for i in range(21)]   # 0.95..1.95
            m = conf = None
            for cand in (self._check_scales, full):
                if not cand:
                    continue
                tpl.scales = list(cand)
                m, conf = find(frame_gray, tpl)
                if m.found:
                    sc = getattr(m, "scale", None)
                    if sc is not None and sc not in self._check_scales:
                        self._check_scales.insert(0, sc)
                        del self._check_scales[3:]
                        self.log.info("mission: green check found at scale %s "
                                      "(cached for next time)", sc)
                    break
            if not m.found:
                self.log.warning("mission: %s up but green check not located "
                                 "(best %.3f); not guessing a click", why, conf)
                return False
            self.actor.click_pixel(*m.center,
                                   why=f"dismiss {why} via green check ({conf:.3f})")
            return True
        finally:
            tpl.scales, tpl.threshold = saved_scales, saved_thr

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

    # Traversal geometry, shared with the Kekkai runner: the canvas at the
    # pinned viewport spans x 760..2680, and the walkable ground is at y 880.
    CANVAS_X0, CANVAS_X1, GROUND_Y = 760, 2680, 880
    EDGE_MARGIN = 40

    def _skip_dialogue(self, payload, max_screens=25):
        """Click through a whole run of dialogue in one go.

        A cutscene is usually SEVERAL screens, and handling one click per pass of
        the main loop paid a full classify() for each - and classify is the
        expensive part, not the click. Draining the run here keeps the cost to
        one cheap template check per screen instead.

        It stops the moment the dialogue does: on anything that is no longer a
        cutscene, so combat, a panel or traversal takes over immediately rather
        than after another full cycle. `max_screens` is a runaway guard, not an
        expectation.
        """
        from perceive import find
        cond = self.conditions.get("cutscene_continue")
        tpl = self.templates.get("cutscene_continue")
        n = 0
        while n < max_screens:
            if self.controls is not None and not self.controls.wait_if_paused():
                return n
            if payload is not None and getattr(payload, "center", None):
                self.actor.click_pixel(*payload.center,
                                       why=f"advance dialogue {n + 1}")
            elif tpl is not None:
                break
            n += 1
            self.stats["cutscenes"] += 1
            time.sleep(self.dialogue_settle)
            bgr = self.capture.frame(gray=False)
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            # Cheap check FIRST: is another dialogue screen up? Only if not do we
            # hand back, so the expensive full classify runs once per run of
            # dialogue rather than once per screen.
            payload = cond.check(bgr, gray) if cond is not None else None
            if not payload:
                break
        if n:
            self.log.info("mission: advanced %d dialogue screen(s)", n)
        return n

    @staticmethod
    def _scene_hash(frame_bgr):
        """A coarse fingerprint of the MAP AREA, for detecting movement.

        Excludes the HUD and the bottom bar: those never change and would only
        dilute the signal.
        """
        a = frame_bgr[300:1150, 780:2660]
        if a.size == 0:
            a = frame_bgr
        g = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        s = cv2.resize(g, (32, 18), interpolation=cv2.INTER_AREA)
        return s > s.mean()

    @staticmethod
    def _scene_changed(before, after, thr=0.10):
        """Did the run actually take us somewhere?

        Measured over five real runs on "Desert Ronins":

            moved / map changed   0.161, 0.193, 0.557
            went nowhere          0.010, 0.066

        so 0.10 sits in the gap. A whole-frame mean-absolute-difference does NOT
        separate these anywhere near as cleanly (CLAUDE.md records a real map
        change measuring only 0.053 there, because the scenes are similarly lit);
        a coarse light/dark hash does.
        """
        if before is None or after is None or before.shape != after.shape:
            return True
        return float((before != after).mean()) >= thr

    def _traverse(self, frame_bgr):
        """Walk. Encounters trigger on MOVEMENT, so standing still stalls a
        mission with no error at all - which is exactly what was happening: the
        bot sat on a traversal screen logging "observing only" while the mission
        waited for it to move.

        RUN TO A MAP EDGE, DO NOT POKE AT THE MIDDLE. CLAUDE.md records this from
        the Kekkai work: clicking mid-ground just shuffles the character around
        one map forever. The way onward is the left or right edge, and the
        location changes during the run.

        HEADING IS ALTERNATED, NOT DERIVED. The Kekkai runner picks a heading
        from where the character is standing, because you spawn near the edge you
        entered through - but it finds the character by its RED ROBE, and at
        Lv 65 this character wears purple, so that blob search returns None and
        the heading would be a coin flip dressed up as a measurement. Alternating
        on a dead end reaches the same place without pretending to know: a wrong
        first guess costs one run, and the next one goes the other way.

        The wait is a GATE, not a sleep, so an ambush mid-run is picked up the
        moment the command bar appears rather than after a fixed delay.
        """
        heading = getattr(self, "_heading", "right")
        x = (self.CANVAS_X1 - self.EDGE_MARGIN if heading == "right"
             else self.CANVAS_X0 + self.EDGE_MARGIN)
        self.log.info("mission: traversing - running %s to the map edge", heading)
        before = self._scene_hash(frame_bgr)
        self.actor.click_pixel(x, self.GROUND_Y, why=f"run {heading} to map edge")

        # Anything that means "stop walking" - in priority order, as everywhere.
        watch = [c for c in (self.conditions.get("command_bar"),
                             self.conditions.get("mission_success"),
                             self.conditions.get("result_panel"),
                             self.conditions.get("cutscene_continue"))
                 if c is not None]
        if watch:
            got = self.gate.wait_for_any(watch, timeout=self.traverse_settle,
                                         why="traverse")
            if got:
                self.log.info("mission: traversal interrupted by %s", got.name)
                self._heading = heading      # it worked; keep going this way
                return
        else:
            time.sleep(self.traverse_settle)

        # No ambush. That does NOT mean the run failed - MOVING WITHOUT MEETING
        # ANYTHING IS THE NORMAL CASE. Flipping heading every quiet run made the
        # bot ping-pong between the two edges forever, which is what the messy
        # navigation looked like: right, left, right, left, never getting
        # anywhere. Only turn round when the scene did not change, which means
        # we genuinely could not go that way.
        self._traverse_runs = getattr(self, "_traverse_runs", 0) + 1
        after = self._scene_hash(self.capture.frame(gray=False))
        if self._scene_changed(before, after):
            self.log.info("mission: moved on (run %d); still heading %s",
                          self._traverse_runs, heading)
            self._heading = heading
            return
        self._heading = "left" if heading == "right" else "right"
        self.log.info("mission: that way is a dead end (run %d); turning %s",
                      self._traverse_runs, self._heading)


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
