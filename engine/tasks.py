#!/usr/bin/env python3
"""What a task IS, stated once, in one place.

WHY THIS EXISTS
---------------
`Runner.step` used to be an if/elif chain over four task-name strings, with
`farm_missions`' pre-flight logic (am I already in a mission? is this a
traversal map?) inlined into the GENERIC path before the resume ladder ran.
Adding or changing a task meant editing `step` in two separate places, plus
the `TASKS` list, plus the reset logic, and every task hand-set `mode` and
`note` on its way out. Nowhere did the code say what a task was, so the answer
had to be reassembled by reading the whole method.

That is also why switching tasks felt like something you did to the PROCESS
rather than to the bot: the process owned the connection, the panel and the
work, all with one lifetime, so changing the work looked like restarting
everything.

THE THREE LIFETIMES, SEPARATED
------------------------------
    the page session   long-lived and IRREPLACEABLE - the bot cannot recreate
                       the session cookie, so it must outlive every task
    the panel          injected into the page, already survives the process
                       (which is why killing the process leaves a zombie panel)
    the task           cheap, interruptible, swappable - a VALUE, not a process

So a task is a value the supervisor swaps. Nothing here starts, stops or knows
about processes.

THE INTERFACE
-------------
    preflight(rt) -> bool    handle this cycle BEFORE the resume ladder runs;
                             True means "handled, do not run the ladder". This
                             is where "I am already in a mission" belongs - a
                             task that can start mid-mission must say so
                             itself rather than the supervisor knowing about
                             one particular task.
    run(rt) -> str|None      one cycle of work, from the lobby. The returned
                             string becomes the panel's note.
    oneshot                  True: finishing is an ENDING, so hand back and
                             go quiet. False: finishing is one lap, so the
                             supervisor comes straight round again.
    needs_lobby              False skips the ladder entirely (only `idle`).

`rt` is the Runner. Tasks reach into it for the capture, actor, log and config
because the Runner IS the context - passing a narrower object would mean
inventing a second one that has to be kept in step with it.
"""


class Task:
    key = ""
    label = ""
    oneshot = False
    needs_lobby = True

    def preflight(self, rt):
        return False

    def run(self, rt):
        raise NotImplementedError

    # NOT EVERY TASK NEEDS A BUTTON IN THE TASK ROW. A hidden task is still
    # a real, runnable task - `BY_KEY` keeps it and `run_task` can start it -
    # it simply has its own control somewhere the operator will actually look
    # for it. The Eudemon scan lives above the boss list it fills, and a
    # second button in the task row would be the same command twice, with the
    # duplicate sitting where its effect is invisible.
    hidden = False

    def as_dict(self):
        return {"key": self.key, "label": self.label}


class Idle(Task):
    key, label = "idle", "Idle"
    needs_lobby = False

    def preflight(self, rt):
        rt.state = "idle"
        return True

    def run(self, rt):
        return None


class ResumeToLobby(Task):
    """Climb to the lobby and stop. A one-shot by its nature."""

    key, label = "resume_to_lobby", "Resume to lobby"
    oneshot = True

    def run(self, rt):
        return "arrived in the lobby"


class TpTraining(Task):
    """One TP pass: play the whole day's list, then hand back.

    The pass itself decides when it is finished - it keeps taking startable
    rows until every one is played or measured to be greyed out. There is no
    mission count here on purpose; see `tp.run_all`.
    """

    key, label = "tp_training", "TP training"
    oneshot = True

    def run(self, rt):
        import tp as tp_mod
        rt.note = "TP run in flight - the panel pauses until it finishes"
        rt.push()
        # Play whatever is listed, identifying each minigame from the screen.
        # Names are not used to choose: the family a title implies is not
        # guaranteed to be the minigame you get, and a name-matched picker
        # silently skips anything renamed or newly added.
        played, banked = tp_mod.run_all(rt.cap, rt.actor, rt.log,
                                        relog=rt.relog)
        return f"TP pass: {played} started, {banked} banked"


class SsTraining(Task):
    """One SS pass: play the whole day's SS list, then hand back.

    THE NEXT TIER UP FROM TP, and the operator asked for it as its own button
    rather than something reached through the farm. That is not only
    convenience: an SS mission that turns out to be COMBAT used to leave the
    supervisor in `farm_missions`, which then went off and started a story
    mission - "it always returns to farm mission after combat".

    Same shape as TpTraining: a one-shot over the day's list, terminating when
    every row is played or measured to be greyed out rather than on a count.

    SS MIXES PUZZLES AND FIGHTS, and which is which is read off the screen:
    `Sage Power Seal` is the rune Mastermind, `Twins Unicorn` is two Lv 80
    enemies. Combat is handed to the supervisor's OWN mission runner, so SS
    fights inherit the whole battle stack - the panel's rotation, the
    greyed-slot skip, cooldown learning, the stun fallback to Dodge and
    DamageWatchdog - instead of a second copy of it.
    """

    key, label = "ss_training", "SS training"
    oneshot = True

    def run(self, rt):
        import ss as ss_mod
        rt.note = "SS run in flight - the panel pauses until it finishes"
        rt.push()
        played, banked = ss_mod.run_all(rt.cap, rt.actor, rt.log,
                                        tpls=rt.tpls,
                                        play_combat=rt._run_mission,
                                        relog=rt.relog)
        return f"SS pass: {played} started, {banked} banked"



class EudemonHunt(Task):
    """Farm the Eudemon Garden boss ladder until nothing will start.

    The operator's shape for this: run every boss, with a BLACKLIST of ones to
    skip, and keep going until each is exhausted. The blacklist covers only
    the non-SS ranks - SS bosses are time limited, so `eudemon.blacklistable`
    refuses to skip one whatever the blacklist says.

    **Fights use the HUNT rotation** (`profile="hunt"`), which is a second
    skill order the panel keeps beside the main one. Bosses are a different
    fight from a story mission and the operator asked for them to be declared
    separately; when that list is empty the main order is used rather than
    dropping to Attack-only, because a boss fight with no rotation is the
    worst possible default.

    `needs_lobby` stays True: the garden is reached from the village, and the
    resume ladder can always get there.
    """

    key, label = "eudemon_hunt", "Eudemon hunt"
    oneshot = True

    def run(self, rt):
        import eudemon as eu
        rt.note = "Eudemon hunt in flight - the panel pauses until it finishes"
        rt.push()
        roster = rt.eudemon_roster_fps()
        fought, banked = eu.hunt(
            rt.cap, rt.actor, rt.log,
            play_combat=lambda: rt._run_mission(profile="hunt"),
            blacklist=rt.eudemon_blacklist(),
            relog=rt.relog, cdp=rt.cdp,
            roster=roster, skip_keys=rt.eudemon_skip_keys(),
            on_roster=rt.save_eudemon_roster)
        return f"Eudemon: {fought} fought, {banked} banked"


class EudemonScan(Task):
    """Read the CURRENT boss list into the panel, without fighting anything.

    **The list changes with events**, so which bosses exist is not a fixed
    fact to be learned once. The operator asked to be able to scan what is
    there now and then allocate the skips by hand, and this is that button:
    it pages the whole garden, refreshes the roster, and comes back.

    It is a `replace` survey, not an additive one. `harvest_roster` only ever
    ADDS, so without this the panel would keep offering bosses that an event
    has taken away. Identity still travels by FINGERPRINT, so a boss that
    survives the rescan keeps its key and its name - and therefore its
    blacklist entry - while a genuinely new one gets a fresh key and a retired
    index is never handed to a different boss.

    Deliberately a separate task from the hunt. Scanning is cheap, safe and
    reversible; fighting is none of those, and an operator who wants to see
    what is available should not have to start a fight to find out.
    """

    key, label = "eudemon_scan", "Scan bosses"
    oneshot = True
    # Its button is the "Scan bosses now" one directly above the boss list -
    # see `Task.hidden`.
    hidden = True

    def run(self, rt):
        import eudemon as eu
        rt.note = "scanning the Eudemon boss list"
        rt.push()
        if not eu.in_garden(rt.cap.frame(gray=False)):
            eu.dismiss_popups(rt.actor, rt.cap, rt.log)
            if not eu.to_garden(rt.actor, rt.cap, rt.log):
                return "could not reach the Eudemon Garden"
        roster = rt.eudemon_roster_fps()
        eu.survey_roster(rt.cap, rt.actor, rt.log, roster,
                         on_roster=rt.save_eudemon_roster, replace=True)
        rt.save_eudemon_roster(roster)
        eu.close(rt.actor, rt.cap, rt.log)
        skippable = sum(1 for e in roster if eu.blacklistable(e["rank"]))
        return (f"scanned {len(roster)} boss(es), {skippable} can be skipped "
                f"- click them in the panel")


class FarmMissions(Task):
    """Farm story missions, one mission per lap.

    This is the task that can legitimately begin in the middle of its own
    work, so it owns that knowledge rather than the supervisor doing it on
    this task's behalf.
    """

    key, label = "farm_missions", "Farm missions"

    # How many unreadable frames before a scenery-looking screen is treated as
    # a traversal map. What this licenses is a click on the map EDGE, and a
    # map-edge click in the village lands on a building, so it waits for the
    # ladder to have failed repeatedly first.
    WALK_AFTER_UNKNOWN = 3

    def preflight(self, rt):
        # ALREADY IN A MISSION? Then play it, and do not ask the resume ladder
        # to find the lobby first - it cannot, because battles and traversal
        # are deliberately not its job. Demanding the lobby before acting is
        # why a session that began mid-mission logged "no anchor matched"
        # forever while a battle sat waiting for input.
        import farm as farm_mod
        where = rt.guard(lambda: farm_mod.in_mission(rt.cap.frame(gray=False),
                                                     rt.tpls))
        if where:
            rt.state = where
            rt.note = f"mission already in progress ({where}) - playing it"
            rt.log.info("%s", rt.note)
            rt.push()
            rt._run_mission()
            return True

        # A traversal screen has no anchor of its own - it is scenery - so the
        # ladder cannot name it and the bot used to sit there while the mission
        # waited for it to walk.
        if rt.unknown >= self.WALK_AFTER_UNKNOWN:
            scene = rt.guard(
                lambda: farm_mod.looks_like_mission_scene(
                    rt.cap.frame(gray=False), rt.tpls),
                default=False)
            if scene:
                rt.state = "traversal"
                rt.note = ("no anchor anywhere and nothing says village - "
                           "treating this as a mission map and walking")
                rt.log.info("%s", rt.note)
                rt.push()
                rt.unknown = 0
                rt._run_mission()
                return True
        return False

    def run(self, rt):
        # Choose by READING the grade panel rather than by config: the grade
        # bars are colour coded and a locked grade renders grey, so "best
        # available" is a measurement. See engine/farm.py.
        import farm as farm_mod
        rt.note = "mission in flight - the panel pauses until it finishes"
        rt.push()
        started, banked = farm_mod.farm(rt.cap, rt.actor, rt.log,
                                        rt.battle_cfg(), rt.controls,
                                        repeat=1)
        # STARTING A MISSION AND BANKING NOTHING IS NOT PROGRESS. `farm.farm`
        # catches a mission-runner crash, logs it and breaks - so a hard,
        # repeating fault still returns normally from here. Saying so lets the
        # supervisor's setback budget actually accumulate instead of being
        # cleared on every cycle by a lap that achieved nothing.
        rt._progress = banked > 0
        return f"farm: {started} started, {banked} banked"


class ExamKekkai(Task):
    """Solve an exam's rune puzzle from wherever the operator has got to.

    WHAT WAS ALREADY DONE, AND WHY THIS IS SMALL. The reference bot solved this
    puzzle for the Jounin and Sage exams and never for TP - the opposite of us -
    and porting "the exams" turned out to need almost no new machinery, because
    every piece of the path is already context-free:

      * `minigame.classify` reads the family OFF THE SCREEN rather than from a
        configured label, so an exam seal is recognised as a kekkai already;
      * `kekkai_play` contains no TP assumptions at all;
      * `hunt_and_solve` COUNTS THE SEAL'S NODES and uses that as the code
        length (2..6), which is exactly the range the exam uses - four
        coordinate tables keyed 2..5 in their bot. TP only ever showed 3 and 5.
      * `kekkai.candidates` is generic in the length, being a plain product over
        the six runes.

    So the puzzle is supported. WHAT IS NOT is the navigation to reach an exam,
    and that cannot be written without seeing the screens - inventing templates
    for a menu nobody has looked at is the eyeball mistake this project keeps
    paying for. `needs_lobby` is False for the same reason the farm can start
    mid-mission: the resume ladder deliberately cannot name an exam screen, so
    demanding the lobby first would make this unusable.

    Until the navigation exists the division of labour is: the operator gets to
    the exam, presses Run, and the bot plays the puzzle. When it finds nothing
    it SAVES THE SCREEN, which is how the navigation gets taught - the same
    trick that eventually solved the mission list, the between-turns battle and
    the Level Up panel.
    """

    key, label = "exam_kekkai", "Exam (rune puzzle)"
    oneshot = True
    needs_lobby = False

    def preflight(self, rt):
        import minigame as mg
        frame = rt.cap.frame(gray=False)
        kind, ev = mg.classify(frame)
        rt.state = f"exam:{kind}"

        if kind == mg.KEKKAI:
            rt.note = "exam rune puzzle on screen - solving"
            rt.log.info("%s (%s)", rt.note, ev)
            rt.push()
            kind, ok = mg.solve(rt.cap, rt.actor, rt.log, frame=frame)
            rt.note = ("exam puzzle solved" if ok else
                       "exam puzzle not completed - see the log")
            rt.log.info("%s", rt.note)
            return True

        # Nothing playable here. Say so precisely, and keep the frame: an exam
        # screen we cannot name is one anchor away from navigable, and the hard
        # part is always CATCHING it.
        rt.note = (f"no rune puzzle on this screen ({kind}) - navigate to the "
                   f"exam and press Run")
        rt.log.info("%s", rt.note)
        rt.log.info("exam navigation is not implemented: it needs the exam's own "
                    "screens, which nobody has captured yet. Saving this frame "
                    "so it can be taught.")
        try:
            rt._save_for_teaching()
        except Exception as e:
            rt.log.warning("could not save the frame: %s", e)
        return True

    def run(self, rt):
        return None


# ORDER IS THE PANEL'S ORDER. `idle` sits last because it is the resting
# choice, not the first thing an operator wants to reach for.
REGISTRY = [ResumeToLobby(), TpTraining(), SsTraining(),
            EudemonScan(), EudemonHunt(),
            FarmMissions(),
            ExamKekkai(), Idle()]
BY_KEY = {t.key: t for t in REGISTRY}
# The PANEL's task row. Hidden tasks are runnable but have their own control
# elsewhere, so they are deliberately absent here - validate a command
# against BY_KEY, never against this list, or a hidden task becomes
# unreachable by the very button that exists to run it.
AS_DICTS = [t.as_dict() for t in REGISTRY if not t.hidden]


def get(key):
    """The task for `key`, falling back to Idle rather than raising.

    An unknown key is an operator-input problem, and the safe response is to
    do nothing rather than to take down a session that holds an
    irreplaceable login.
    """
    return BY_KEY.get(key, BY_KEY["idle"])
