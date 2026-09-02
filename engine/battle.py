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
import os
import time

import cv2

import combat
import gate as gate_mod
from gate import Stopped
from geometry import BattleGeometry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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

    def __init__(self, order, tracker, log, kinds=None, mode="priority",
                 fallback=None):
        if not order:
            raise ValueError("skill rotation is empty; configure at least one slot")
        self.order = list(order)
        self.tracker = tracker
        self.log = log
        # "priority" keeps the configured order fixed and simply takes the first
        # slot that is READY, which is what an operator means by "press these in
        # this order, and Attack when none are left". "rotate" is the older
        # round-robin, kept because it is the only sane behaviour for a slot
        # whose cooldown length is unknown - see `resolved`.
        self.mode = mode
        # What to do when every configured slot is cooling. Attack always works
        # and is the only action measured to reliably deal damage on this
        # client, so a fight never stalls just because the skills are down.
        self.fallback = fallback
        # slot -> "damage" | "buff" | "heal" | ... purely informational here, but
        # it keeps a buff-only slot from being counted as a damage attempt.
        self.kinds = dict(kinds or {})

    def turn_ended(self, something_resolved):
        """Close the books on this turn, now that its outcome is known.

        A failure only bounds a cooldown if the turn was otherwise healthy. If
        NOTHING resolved we were probably stunned or frozen, and every failure
        this turn is uninformative - admitting them would invent lower bounds
        and eventually DISABLE A WORKING SKILL, which is much worse than
        learning nothing.
        """
        pending = getattr(self, "_failed_this_turn", [])
        self._failed_this_turn = []
        for slot in pending:
            learned = self.tracker.record(slot, False,
                                          turn_was_healthy=something_resolved)
            if learned:
                self.log.info("rotation: LEARNED %s has a %d-round cooldown "
                              "(from a failure on an otherwise healthy turn)",
                              slot, learned)
            elif something_resolved:
                lo, hi = self.tracker.bracket(slot)
                if lo is not None or hi is not None:
                    self.log.info("rotation: %s cooldown is between %s and %s "
                                  "rounds so far", slot, lo, hi)

    def candidates(self):
        """Offerable slots, in preference order, cooling ones removed.

        The fallback is appended last so there is ALWAYS something to try: with
        every skill cooling the caller would otherwise have nothing to click and
        the turn would pass doing nothing.
        """
        out = []
        for slot in self.order:
            ready = self.tracker.ready(slot)
            if ready is False:
                continue          # known to be cooling — skip
            out.append(slot)      # True, or None == unknown, so offer it
        if self.fallback and self.fallback not in out:
            out.append(self.fallback)
        return out

    def resolved(self, slot):
        """Called when `slot` actually consumed our turn.

        Records the use for round bookkeeping AND demotes the slot to the back of
        the queue. Both, not either: bookkeeping is exact where we know the
        cooldown, and demotion covers the slots where we do not.
        """
        learned = self.tracker.record(slot, True)
        if learned:
            self.log.info("rotation: LEARNED %s has a %d-round cooldown - it "
                          "will be withheld until ready instead of clicked and "
                          "waited on", slot, learned)
        # In PRIORITY mode a slot with a KNOWN cooldown keeps its place: the
        # tracker will withhold it until it is ready again, so the order can stay
        # exactly as configured. A slot whose cooldown we do NOT know still has
        # to be demoted, or priority would press it every single turn forever -
        # bookkeeping is the only thing that can stop that, and we have none.
        known = self.tracker.ready(slot) is not None
        if self.mode == "priority" and known:
            return
        if slot in self.order:
            self.order.remove(slot)
            self.order.append(slot)

    def failed(self, slot):
        """Called when clicking `slot` did nothing observable.

        Same demotion, and the failure is REMEMBERED rather than acted on: on
        its own it does not say why. A skill can fail because it is cooling,
        because we are stunned, because CP ran out, or because the click
        missed - and only the first of those says anything about a cooldown.
        What separates them is whether anything ELSE resolved this turn, which
        is not known until the turn is over. So it waits for `turn_ended`.
        """
        self._failed_this_turn = getattr(self, "_failed_this_turn", [])
        self._failed_this_turn.append(slot)
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
        # "run" restores the old flee-on-watchdog behaviour. It defaults to
        # "fight" because fleeing FAILS the mission.
        self.watchdog_action = c.get("watchdog_action", "fight")
        # What to press when NOTHING else resolves. Dodge, because a stun or
        # similar restriction disables every other action and leaves it as the
        # only thing the game will accept.
        self.restricted_action = c.get("restricted_action", "DO")
        # Consecutive non-resolving actions in ONE turn before we stop probing
        # and take `restricted_action`. Two, because one failure is ambiguous
        # (cooldown, bad click) while two in a row is the stun signature.
        self.RESTRICTED_AFTER = int(c.get("restricted_after", 2))
        self._warned_watchdog = False
        self.action_timeout = c.get("action_timeout_s", 6)
        self.max_rounds = c.get("max_rounds", 60)
        self.target_policy = c.get("target_policy", "lowest_hp")
        self.closing_action = c.get("closing_action")     # 'AT' | 'CH' | 'DO' | None

        self.tracker = combat.CooldownTracker(
            cfg.get("combat", {}).get("cooldowns", {}).get("rounds_per_slot", {}))
        self.watchdog = combat.DamageWatchdog(
            stall_turns=c.get("watchdog_stall_turns", 3))
        self.rotation = SkillRotation(c.get("rotation", []), self.tracker, log,
                                      mode=c.get("order_mode", "priority"),
                                      fallback=c.get("fallback", "AT"),
                                      kinds=c.get("slot_kinds"))

    # -- geometry ------------------------------------------------------------
    def _geometry(self, frame_gray):
        return BattleGeometry.locate(frame_gray,
                                     self.templates["charge_btn"],
                                     self.templates["dodge_btn"])

    # -- the loop ------------------------------------------------------------
    def run(self):
        """Returns (outcome, info dict)."""
        # Tighten click pacing for the duration of the fight. The Actor's
        # human-like defaults sleep up to 1.65 s per click, which in a battle is
        # most of the gap between the turn gate firing and the action going out.
        from act import fast_pacing
        with fast_pacing(self.actor):
            return self._run()

    def _run(self):
        rounds = 0
        acted = 0
        while rounds < self.max_rounds:
            # Priority order matters: the result panel draws OVER the command
            # bar, so it must be tested first or a finished fight reads as
            # "my turn". CLAUDE.md calls this out explicitly.
            waits = [self.conditions["result_panel"]]
            if "defeat_panel" in self.conditions:
                waits.append(self.conditions["defeat_panel"])
            # A FIGHT CAN END STRAIGHT INTO DIALOGUE, with no Victory panel at
            # all. Observed on "Desert Ronins": the last enemy fell and the game
            # cut to "These ronins are stronger than we thought", while this gate
            # waited 45 s for a turn that could never come and then reported the
            # battle stalled. A cutscene is an end condition, not a stall.
            if "cutscene_continue" in self.conditions:
                waits.append(self.conditions["cutscene_continue"])
            waits.append(self.conditions["command_bar"])

            fired = self.gate.wait_for_any(waits, self.turn_timeout,
                                           why=f"battle turn {rounds + 1}")
            if fired and fired.name == "cutscene_continue":
                self.log.info("battle: ended into dialogue after %d round(s)",
                              rounds)
                return VICTORY, {"rounds": rounds, "acted": acted,
                                 "ended": "cutscene"}
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
                # THE GATE HAS ALREADY SAID THE COMMAND BAR IS THERE, which
                # invalidates the fast path's whole assumption. `locate` budgets
                # its misses on the premise that a miss means the bar is absent
                # - true on a cutscene or a panel, false here. So force one full
                # re-acquire instead of trusting the budget.
                #
                # Without this, a single budgeted miss ended the MISSION: live,
                # a fight went "gate -> command_bar (0.44s)" then immediately
                # "geometry failed -> stalled", and the runner fell back to the
                # resume ladder, walked, met the next encounter and stalled the
                # same way - a loop that never finished a battle.
                self.log.info("battle: geometry missed a gated command bar - "
                              "forcing a full re-acquire")
                BattleGeometry.forget()
                geo = self._geometry(gray)
            if geo is None:
                self.log.warning("battle: command bar gated but geometry failed")
                return STALLED, {"rounds": rounds, "acted": acted}

            verdict = self._observe_progress(bgr, geo)
            if verdict in ("stalled", "regenerating"):
                # TAKING `Run` FAILS THE MISSION. That is a bad trade for
                # ordinary farming: a regenerating enemy usually still dies with
                # more rounds, whereas fleeing throws away the whole mission and
                # the stamina behind it. Observed live - the watchdog fled a
                # story mission on `regenerating` and the mission was lost.
                #
                # So the default is to keep fighting and say so. `max_rounds`
                # still bounds the fight, so a genuinely unwinnable one ends as
                # STALLED rather than running forever, and the mission runner's
                # own `max_battles` and `max_steps` bound it again above that.
                # Set combat.watchdog_action = "run" to restore fleeing.
                if self.watchdog_action != "run":
                    if not self._warned_watchdog:
                        self.log.warning(
                            "battle: watchdog=%s - NOT fleeing, because Run "
                            "fails the mission. Fighting on; max_rounds=%d "
                            "still bounds this.", verdict, self.max_rounds)
                        self._warned_watchdog = True
                else:
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

        Feeds TOTAL enemy HP and the enemy count, not the lowest bar - see the
        note in the body and in DamageWatchdog.observe.
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
        # TOTAL, not lowest. The lowest bar jumps UP the moment the weakest
        # enemy dies - measured 18.4 -> 39.7 on a fight that was being won - and
        # the watchdog read that as regeneration and fled. Total enemy HP falls
        # both when an enemy is damaged and when one is killed, and the count
        # makes a kill count as progress on its own.
        total = sum(f for _, f in bars)
        v = self.watchdog.observe(total, enemy_count=len(bars))
        self.log.info("battle: enemies=%d total=%.1f%% lowest=%.1f%% "
                      "watchdog=%s bars=%s", len(bars), total,
                      min(f for _, f in bars), v,
                      [f"{f:.1f}" for _, f in bars])
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

        # STOP PROBING ONCE RESTRICTION IS OBVIOUS. A stunned player has every
        # action greyed out except Dodge, and clicking a disabled button does
        # nothing - so each candidate burns a full resolve timeout (~6 s) to
        # tell us what the previous one already did. Measured live: S4, S5 and
        # S1 each timed out in one round, ~24 s to reach a Dodge that was always
        # the only legal move.
        #
        # One failure is ambiguous (a cooldown, a bad click). TWO consecutive
        # failures in the same turn is the signature of a restriction, so break
        # out and take the restricted action instead of finishing the rotation.
        # If the restriction is real, Dodge resolves immediately and the turn
        # ends; if it was not, the next turn tries the rotation again from the
        # top, so nothing is permanently given up.
        misses = 0
        fallback = getattr(self.rotation, "fallback", None)
        skipped = False
        for slot in self.rotation.candidates():
            if (misses >= self.RESTRICTED_AFTER and self.restricted_action
                    and slot != fallback):
                # Stop probing SKILLS - but never skip the fallback attack.
                #
                # `candidates()` appends the fallback LAST, so an early `break`
                # here jumped straight past Attack to Dodge. With skills merely
                # on COOLDOWN that is the wrong action entirely: the bot spent
                # its turn dodging while Attack was available and would have
                # dealt damage.
                #
                # Cooldown and restriction are distinguishable, and this is how:
                # a cooldown disables only the skill itself, whereas a stun
                # disables everything EXCEPT Dodge. So try Attack; if that
                # resolves it was cooldowns, and if it fails too the restriction
                # is real and the Dodge below is right.
                skipped = True
                continue
            point = geo.slot(slot) if slot.startswith("S") else geo.cmd(slot)

            # DO NOT CLICK A GREYED SKILL AT ALL. The game draws a cooling
            # skill in true greyscale - measured saturation 0.0 inside the
            # tile against 164.0 for a ready one - so the answer to "may I use
            # this" is on screen before we touch anything.
            #
            # This is the operator's actual complaint: a cooling skill was
            # clicked, the click did nothing, and the turn then burned the full
            # ~6s resolve timeout before falling through to Attack. Twice in a
            # turn is 12s of standing still. Reading the tile removes the click
            # rather than making the wait shorter.
            #
            # An UNKNOWN reading (None - off-frame, degenerate crop) offers the
            # slot anyway: the cost of trying is one timeout, while the cost of
            # wrongly withholding is a skill that never fires again.
            if slot.startswith("S"):
                if combat.slot_cooling(frame_bgr, point):
                    self.log.info("battle: %s is greyed out (cooling) - not "
                                  "clicking it", slot)
                    continue

            self.actor.click_pixel(*point, why=f"action {slot} (round {rounds})")

            # Two-step: action, then target. CLAUDE.md records this as the
            # interaction model ("click action, then click the target").
            if target and self.cfg.get("battle", {}).get("click_target", True):
                tp = geo.target(target)
                self.actor.click_pixel(*tp, why=f"target {target} for {slot}")

            resolved = self._wait_resolved(slot)
            if resolved:
                self.rotation.resolved(slot)
                # Something fired, so this turn was NOT a stun - which is what
                # makes every other failure in it usable evidence.
                self.rotation.turn_ended(True)
                return True
            self.rotation.failed(slot)
            misses += 1
            if misses == self.RESTRICTED_AFTER and not skipped:
                self.log.info("battle: %d actions did not resolve - skipping "
                              "the rest of the rotation, but still trying %s",
                              misses, fallback or "the fallback")

        # NOTHING RESOLVED. The usual cause is not a bad click: the player is
        # STUNNED or otherwise restricted, and every action except Dodge is
        # disabled. Clicking a disabled button does nothing at all, so the turn
        # never resolves and the bot re-clicks Attack forever - observed live on
        # "Desert Ronins", where the log filled with "rotation exhausted" while
        # enemy HP sat flat at 263.7% for a dozen rounds.
        #
        # Dodge is what the game leaves available, so take it. It costs the turn
        # and lets the restriction tick down, which is exactly what a human does.
        if self.restricted_action:
            slot = self.restricted_action
            self.log.info("battle: nothing resolved - the player looks "
                          "restricted (stun?), taking %s", slot)
            self.actor.click_pixel(*geo.cmd(slot),
                                   why=f"restricted -> {slot} (round {rounds})")
            if self._wait_resolved(slot):
                # Dodge resolving while everything else failed is the STUN
                # signature, so this turn teaches nothing about cooldowns.
                # Recording it would invent lower bounds for skills that were
                # never cooling at all.
                self.rotation.turn_ended(False)
                return True

        # Fall back to the configured closing
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
            closed = bool(self._wait_resolved(self.closing_action))
            # A plain attack landing PROVES we were not stunned, so the skills
            # that failed this turn really were cooling - that is the most
            # informative turn there is for learning a cooldown.
            self.rotation.turn_ended(closed)
            return closed
        self.log.warning("battle: no action resolved this turn and no closing "
                         "action configured")
        # NOTHING resolved at all, so nothing is known about why any of it
        # failed. Flush as unhealthy, and flush on EVERY exit: a turn whose
        # failures are never flushed leaves them pending, and they would then
        # be attributed to the NEXT turn's round number - a silent off-by-one
        # in the one place a wrong number disables a working skill.
        self.rotation.turn_ended(False)
        return False

    # The game's own refusal message, if we have a template for it. Named here
    # rather than inline so it is obvious what to cut and where it plugs in.
    COOLDOWN_TPL = "skill_cooldown"
    # How many mystery frames to keep per process. Enough to cut a template
    # from; not enough to fill a disk on a bad night.
    KEEP_FAILED_FRAMES = 3

    def _wait_resolved(self, slot):
        """Did the turn actually end after clicking `slot`?

        "Resolved" means the command bar went away or the fight ended — the same
        signal the reference bot uses. Note this cannot distinguish "the skill
        fired" from "the skill fired and did nothing useful"; the watchdog is
        what catches the latter, one turn later.

        A REFUSAL IS ALSO AN ANSWER, and a much faster one. When a skill is on
        cooldown the game says so in a message at the top of the screen, and
        nothing else happens - so waiting for the command bar to disappear
        means waiting out the whole `action_timeout` (~6 s) to learn what the
        game told us immediately. Two failures in a turn is 12 s of standing
        still.

        So the refusal is watched for alongside the success conditions, and it
        LOSES the race deliberately: whichever fires first ends the wait, and a
        refusal returns False at once.

        This activates by itself when `tpl/skill_cooldown.png` exists, and is
        simply skipped until then - no code change needed to switch it on. The
        template has to be cut from a real frame showing the message, which is
        what `_keep_failed_frame` below is for. Guessing at a crop for a
        message nobody has looked at would be the "measure, don't eyeball"
        mistake this project keeps paying for.
        """
        waits = [self.conditions["result_panel"],
                 self.conditions["command_bar_gone"]]
        cd_tpl = (self.templates or {}).get(self.COOLDOWN_TPL)
        if cd_tpl is not None:
            waits.append(gate_mod.template(self.COOLDOWN_TPL, cd_tpl))
        fired = self.gate.wait_for_any(waits, self.action_timeout,
                                       why=f"resolve {slot}")
        if fired is not None and getattr(fired, "name", "") == self.COOLDOWN_TPL:
            self.log.info("battle: the game says %s is on COOLDOWN - moving to "
                          "the next action instead of waiting out %.1fs",
                          slot, self.action_timeout)
            return False
        if not fired:
            self._keep_failed_frame(slot)
        return bool(fired)

    def _keep_failed_frame(self, slot):
        """Save the screen that refused us, so the message can be templated.

        Every unrecognised screen in this project turned out to be ONE anchor
        from ONE frame away from handled - the mission list, a battle between
        turns, the seal-broken dialog, the Level Up panel. The hard part was
        always CATCHING the frame, and a skill timing out on cooldown is
        exactly the moment the refusal message is on screen.
        """
        if (self.templates or {}).get(self.COOLDOWN_TPL) is not None:
            return                      # already templated; nothing to learn
        n = getattr(self, "_kept_frames", 0)
        if n >= self.KEEP_FAILED_FRAMES:
            return
        try:
            frame = self.capture.frame(gray=False)
            d = os.path.join(ROOT, "ref/auto/battle")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"cooldown_msg_{slot}_{int(time.time())}.png")
            cv2.imwrite(path, frame)
            self._kept_frames = n + 1
            self.log.info("battle: %s timed out - saved %s. If the game drew "
                          "an 'on cooldown' message, crop it to "
                          "tpl/%s.png and this wait gets ~%.0fs shorter.",
                          slot, os.path.relpath(path, ROOT), self.COOLDOWN_TPL,
                          self.action_timeout)
        except Exception as e:
            self.log.warning("battle: could not save the refusal frame: %s", e)
