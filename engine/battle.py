"""Battle executor — one encounter, fought to a verdict.

This is the reference bot's battle loop (`ref/tp/cmmhero`, FormMain.cs around
6900-7160) rebuilt on our stack, with its two real weaknesses fixed.

WHAT WE TAKE FROM IT
--------------------
1. Gate on state, never on a clock. Every action is followed by "wait until one
   of these known things is true", never by a sleep. See gate.py.
2. Slot-based targeting. It clicks eight fixed battlefield slots, never sprites
   or name plates. geometry.py establishes that the same ring exists here.
3. Rotate-on-resolve. It has no working cooldown detection at all — its
   `CheckSkillCD` is stubbed `return true` (FormMain.cs:6904). Instead, when a
   skill click resolves the turn, that slot is moved to the BACK of the queue.
   Queue order becomes the cooldown proxy: a used skill cannot come up again
   until everything else has been tried. Zero calibration required.

WHAT WE FIX
-----------
1. It reads no HP anywhere in the battle path, so it cannot tell a slow win from
   an unwinnable fight. CLAUDE.md records the case that breaks it: enemy HP
   observed going 50.7 -> 43.0 -> 43.0 -> 43.0 -> 47.2 percent. It went UP.
   Enemies regenerate, and an ~8pp-per-hit basic attack can be fully cancelled,
   producing a fight with no error and no end condition. `DamageWatchdog` aborts
   those via Run. Their only failsafe is a wall-clock "stuck" timer, which would
   never fire here because the screen keeps changing.
2. Rotate-on-resolve is the FALLBACK, not the primary. The game's own client
   decrements cooldowns once per round (`nextRound()` -> `reduceSkillCooldown(1)`),
   so cooldowns are exact small integers and are trackable by bookkeeping.
   `CooldownTracker` is consulted first; rotation covers slots whose cooldown
   length we have not measured yet.

WHAT IS STILL UNVERIFIED — read before trusting this live
---------------------------------------------------------
* That clicking a ring slot selects that target. Inferred, not confirmed. See
  geometry.py. If it turns out wrong, `_take_action` is the single place to change.
* Which slots deal damage. CLAUDE.md is explicit that slots are TYPED, not
  uniform: right-bank slot 1 applied Strengthen(1) to self for 50 CP — a buff,
  no damage — and costs ranged ~10 to ~100 CP. So the rotation is CONFIGURED,
  not discovered. An unconfigured slot is never clicked.
* Whether a failed action is distinguishable from a slow one. We treat
  "turn did not resolve inside `action_timeout`" as failure and rotate. That is
  the reference bot's own heuristic and it is not proven correct here.
"""
import cv2

import combat
from gate import Stopped
from geometry import BattleGeometry


# Outcomes. Deliberately explicit strings — they end up in logs and counters.
VICTORY = "victory"
DEFEAT = "defeat"
ABORTED = "aborted"       # we chose to Run (watchdog)
STALLED = "stalled"       # gates stopped resolving; caller must recover
STOPPED = "stopped"       # operator asked us to stop


class SkillRotation:
    """Which action to take this turn.

    Order is the caller's configured preference. A slot is skipped when
    `CooldownTracker` knows it is still cooling; slots with an unknown cooldown
    length are always offered and demoted by `resolved()` after they fire.
    """

    def __init__(self, order, tracker, log, kinds=None):
        if not order:
            raise ValueError("skill rotation is empty; configure at least one slot")
        self.order = list(order)
        self.tracker = tracker
        self.log = log
        # slot -> "damage" | "buff" | "heal" | ... purely informational here, but
        # it keeps a buff-only slot from being counted as a damage attempt.
        self.kinds = dict(kinds or {})

    def candidates(self):
        """Offerable slots, in preference order, cooling ones removed."""
        out = []
        for slot in self.order:
            ready = self.tracker.ready(slot)
            if ready is False:
                continue          # known to be cooling — skip
            out.append(slot)      # True, or None == unknown, so offer it
        return out

    def resolved(self, slot):
        """Called when `slot` actually consumed our turn.

        Records the use for round bookkeeping AND demotes the slot to the back of
        the queue. Both, not either: bookkeeping is exact where we know the
        cooldown, and demotion covers the slots where we do not.
        """
        self.tracker.use(slot)
        if slot in self.order:
            self.order.remove(slot)
            self.order.append(slot)

    def failed(self, slot):
        """Called when clicking `slot` did nothing observable.

        Same demotion, but no cooldown bookkeeping — we did not spend it. Most
        likely it was on cooldown or we lacked the CP.
        """
        if slot in self.order:
            self.order.remove(slot)
            self.order.append(slot)
        self.log.info("rotation: %s did not resolve; demoted -> %s",
                      slot, self.order)


class BattleRunner:
    """Fight one encounter to a verdict.

    Needs, from the caller:
      gate      — engine.gate.Gate
      actor     — engine.act.Actor
      templates — dict of loaded Templates; must include charge_btn + dodge_btn
      conditions— dict of named gate Conditions for the battle states, at minimum
                  'result_panel'. See mission.py for how these are assembled.
    """

    def __init__(self, gate, actor, capture, templates, conditions, cfg, log):
        self.gate, self.actor, self.capture = gate, actor, capture
        self.templates, self.conditions = templates, conditions
        self.cfg, self.log = cfg, log

        c = cfg.get("battle", {})
        self.turn_timeout = c.get("turn_timeout_s", 45)
        self.action_timeout = c.get("action_timeout_s", 6)
        self.max_rounds = c.get("max_rounds", 60)
        self.target_policy = c.get("target_policy", "lowest_hp")
        self.closing_action = c.get("closing_action")     # 'AT' | 'CH' | 'DO' | None

        self.tracker = combat.CooldownTracker(
            cfg.get("combat", {}).get("cooldowns", {}).get("rounds_per_slot", {}))
        self.watchdog = combat.DamageWatchdog(
            stall_turns=c.get("watchdog_stall_turns", 3))
        self.rotation = SkillRotation(c.get("rotation", []), self.tracker, log,
                                      kinds=c.get("slot_kinds"))

    # -- geometry ------------------------------------------------------------
    def _geometry(self, frame_gray):
        return BattleGeometry.locate(frame_gray,
                                     self.templates["charge_btn"],
                                     self.templates["dodge_btn"])

    # -- the loop ------------------------------------------------------------
    def run(self):
        """Returns (outcome, info dict)."""
        rounds = 0
        acted = 0
        while rounds < self.max_rounds:
            # Priority order matters: the result panel draws OVER the command
            # bar, so it must be tested first or a finished fight reads as
            # "my turn". CLAUDE.md calls this out explicitly.
            waits = [self.conditions["result_panel"]]
            if "defeat_panel" in self.conditions:
                waits.append(self.conditions["defeat_panel"])
            waits.append(self.conditions["command_bar"])

            fired = self.gate.wait_for_any(waits, self.turn_timeout,
                                           why=f"battle turn {rounds + 1}")
            if isinstance(fired, Stopped):
                return STOPPED, {"rounds": rounds, "acted": acted}
            if not fired:
                # Timed out. A static frame while awaiting input is NORMAL per
                # CLAUDE.md, so a timeout here means the command bar never
                # appeared at all — genuinely stuck, not merely quiet.
                self.log.warning("battle: no turn and no result in %ss",
                                 self.turn_timeout)
                return STALLED, {"rounds": rounds, "acted": acted}

            if fired.name == "result_panel":
                self.log.info("battle: result panel after %d rounds", rounds)
                return VICTORY, {"rounds": rounds, "acted": acted,
                                 "match": fired.payload}
            if fired.name == "defeat_panel":
                return DEFEAT, {"rounds": rounds, "acted": acted}

            # --- our turn ---------------------------------------------------
            rounds += 1
            self.tracker.next_round()
            bgr = self.capture.frame(gray=False)
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            geo = self._geometry(gray)
            if geo is None:
                self.log.warning("battle: command bar gated but geometry failed")
                return STALLED, {"rounds": rounds, "acted": acted}

            verdict = self._observe_progress(bgr, geo)
            if verdict in ("stalled", "regenerating"):
                self.log.warning("battle: watchdog=%s -> taking Run", verdict)
                self.actor.click_pixel(*geo.cmd("RN"),
                                       why=f"abort: watchdog {verdict}")
                return ABORTED, {"rounds": rounds, "acted": acted,
                                 "watchdog": verdict}

            if self._take_action(bgr, geo, rounds):
                acted += 1

        self.log.warning("battle: hit max_rounds=%d without a verdict",
                         self.max_rounds)
        return STALLED, {"rounds": rounds, "acted": acted, "reason": "max_rounds"}

    # -- progress ------------------------------------------------------------
    def _observe_progress(self, frame_bgr, geo):
        """Feed the watchdog. Returns its verdict.

        We track the BEST (lowest) enemy fill seen, which is what
        DamageWatchdog wants — it counts turns since a new low, precisely so that
        one good early hit cannot mask a fight going nowhere.
        """
        bars = combat.find_enemy_bars(
            frame_bgr,
            x0=int(frame_bgr.shape[1] * 0.55), x1=frame_bgr.shape[1] - 4,
            y0=0, y1=int(frame_bgr.shape[0] * 0.75))
        if not bars:
            # No bar found is not the same as no progress. Do not feed the
            # watchdog a fake reading; a missing measurement must not be able to
            # trigger an abort.
            self.log.info("battle: no enemy HP bar located this turn")
            return "continue"
        lowest = min(f for _, f in bars)
        v = self.watchdog.observe(lowest)
        self.log.info("battle: enemy bars=%s lowest=%.1f%% watchdog=%s",
                      [f"{f:.1f}" for _, f in bars], lowest, v)
        return v

    # -- acting --------------------------------------------------------------
    def _choose_target(self, frame_bgr, geo):
        """Which ring slot to aim at. Returns a slot key or None.

        Only ever returns an ENEMY-side slot. The ally slots T5..T8 are on the
        same ring and clicking one would, at best, waste a turn.
        """
        enemies = geo.enemy_targets(frame_bgr)
        if not enemies:
            return None
        if self.target_policy == "first":
            return enemies[0]
        # 'lowest_hp' would need a per-slot HP reading, which we cannot yet tie
        # to a ring slot — find_enemy_bars gives us bars by y, not by slot. Until
        # that mapping is measured, aim at the first drawn enemy slot and say so
        # rather than pretending to prioritise.
        return enemies[0]

    def _take_action(self, frame_bgr, geo, rounds):
        """Spend our turn. Returns True if something resolved it."""
        target = self._choose_target(frame_bgr, geo)

        for slot in self.rotation.candidates():
            point = geo.slot(slot) if slot.startswith("S") else geo.cmd(slot)
            self.actor.click_pixel(*point, why=f"action {slot} (round {rounds})")

            # Two-step: action, then target. CLAUDE.md records this as the
            # interaction model ("click action, then click the target").
            if target and self.cfg.get("battle", {}).get("click_target", True):
                tp = geo.target(target)
                self.actor.click_pixel(*tp, why=f"target {target} for {slot}")

            resolved = self._wait_resolved(slot)
            if resolved:
                self.rotation.resolved(slot)
                return True
            self.rotation.failed(slot)

        # Nothing in the rotation worked. Fall back to the configured closing
        # action so the turn is not simply abandoned — an unspent turn means the
        # gate will just re-fire and we will spin.
        if self.closing_action:
            self.log.info("battle: rotation exhausted; closing with %s",
                          self.closing_action)
            self.actor.click_pixel(*geo.cmd(self.closing_action),
                                   why=f"closing action {self.closing_action}")
            if target:
                self.actor.click_pixel(*geo.target(target),
                                       why=f"target {target} for closing")
            return bool(self._wait_resolved(self.closing_action))
        self.log.warning("battle: no action resolved this turn and no closing "
                         "action configured")
        return False

    def _wait_resolved(self, slot):
        """Did the turn actually end after clicking `slot`?

        "Resolved" means the command bar went away or the fight ended — the same
        signal the reference bot uses. Note this cannot distinguish "the skill
        fired" from "the skill fired and did nothing useful"; the watchdog is
        what catches the latter, one turn later.
        """
        waits = [self.conditions["result_panel"],
                 self.conditions["command_bar_gone"]]
        fired = self.gate.wait_for_any(waits, self.action_timeout,
                                       why=f"resolve {slot}")
        return bool(fired)
