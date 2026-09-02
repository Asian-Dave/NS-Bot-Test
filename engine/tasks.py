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
        return f"farm: {started} started, {banked} banked"


# ORDER IS THE PANEL'S ORDER. `idle` sits last because it is the resting
# choice, not the first thing an operator wants to reach for.
REGISTRY = [ResumeToLobby(), TpTraining(), FarmMissions(), Idle()]
BY_KEY = {t.key: t for t in REGISTRY}
AS_DICTS = [t.as_dict() for t in REGISTRY]


def get(key):
    """The task for `key`, falling back to Idle rather than raising.

    An unknown key is an operator-input problem, and the safe response is to
    do nothing rather than to take down a session that holds an
    irreplaceable login.
    """
    return BY_KEY.get(key, BY_KEY["idle"])
