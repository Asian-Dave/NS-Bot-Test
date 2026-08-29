#!/usr/bin/env python3
"""One command: a bot window. Game on the left, controls on the right.

    .venv/bin/python engine/app.py

WHAT THIS REPLACES, AND WHY
---------------------------
`dashboard.py` serves a page on 127.0.0.1 and streams captured frames to it.
That works, but the view is only ever as fast as our capture -> encode -> poll
cycle, so it feels laggy and clunky, and it is a second window besides.

This inverts it. Chrome is launched with `--app=<url>`, which drops the tab
strip, the omnibox and the bookmarks bar, leaving a plain application window;
`engine/dock.py` then injects the control panel into that same page, in the
385 CSS px of wallpaper gutter to the right of the game. The operator watches
the REAL canvas at its own framerate - there is no capture in the viewing path
at all - and the buttons sit beside it.

It is worth being precise about what this is not, because "embed the game in a
native app" sounds like it should be possible and is not. Ruffle is WASM +
WebGL, the session cookie lives in this Chrome profile, and CDP is how we click.
Any native shell would have to host a browser engine to run the game and then
expose a debugging protocol to drive it - which is exactly what the reference
bot is (Adobe AIR + CEF: a Chromium in a native frame). So the honest version of
"native window" is a browser window without browser chrome, which is this.

HOW A BUTTON BECOMES AN ACTION
------------------------------
    dock button  ->  window.__nsbot_send(json)      (Runtime.addBinding)
                 ->  Runtime.bindingCalled event    (same CDP socket)
                 ->  cdp.drain_events()             (buffered; see cdp.py)
                 ->  Runner._apply()                here

No HTTP server, no port, nothing to poll. The one prerequisite was fixing
`cdp.py`, which used to discard every message that was not a direct reply - so
events did not reach this codebase at all, silently.

TASKS BLOCK, AND THE PANEL SAYS SO
----------------------------------
A mission takes minutes and has its own internal gates. The cycle loop runs it
to completion rather than trying to interleave, so the panel stops updating
while a task is in flight. That is honest about what is happening; a panel that
kept animating would be pretending to be responsive.

Stop is still immediate: `Controls` is the same file-backed stop switch the rest
of the engine already honours, so a task checks it at its own gates.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2

import browser
import dock as dock_mod
import resume
from act import Actor, Controls
from bot import load_templates
from capture import Capture
from cdp import CDP, CDPError, find_page_target

VIEWPORT = (1720, 720, 2)          # the ONE pinned geometry every template
                                   # threshold in this project was measured at

# What the operator can put in a rotation. AT/CH/DO are the command buttons;
# S1..S8 are the skill slots (4 left bank, 4 right).
SKILL_SLOTS = ["AT", "CH", "DO", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]
SKILLS_PATH = "run/skills.json"
FARM_PATH = "run/farm.json"
GRADES = ["auto", "S", "A", "B", "C"]

TASKS = [
    {"key": "resume_to_lobby", "label": "Resume to lobby"},
    {"key": "tp_training",     "label": "TP training"},
    {"key": "farm_missions",   "label": "Farm missions"},
    {"key": "idle",            "label": "Idle"},
]


class Log:
    """Logs to the terminal AND to the dock's log pane."""

    def __init__(self, sink=None, keep=14):
        self.lines, self.keep, self.sink = [], keep, sink

    def info(self, m, *a):
        msg = (m % a) if a else m
        print("  " + msg, flush=True)
        self.lines.append(f"{time.strftime('%H:%M:%S')} {msg}"[:120])
        del self.lines[:-self.keep]
    warning = error = info


class Disconnected(Exception):
    """The CDP socket died — usually a navigation that tore down the target."""


def attach(port, log, tpls=None, install_dock=True):
    """Build a fresh CDP connection and everything that hangs off it.

    Returns (cdp, cap, actor, dock). Kept separate from `main` so a dropped
    connection can be rebuilt without restarting the process — see
    `Runner.reconnect`.
    """
    t = find_page_target(port=port, url_contains="ninjasaga", timeout=40)
    c = CDP(t["webSocketDebuggerUrl"])
    c.call("Page.enable")
    browser.pin_viewport(c, *VIEWPORT)
    cap = Capture(c)
    actor = Actor(c, cap, log, dry_run=False)
    dk = dock_mod.Dock(c, log)
    if install_dock:
        dk.install(verify=False)
        rect = dk.dock_rect()
        if rect:
            actor.no_click_zones.append(rect)
    return c, cap, actor, dk


class Runner:
    def __init__(self, cdp, cap, actor, tpls, cfg, log, controls, dock,
                 port=9222):
        self.cdp, self.cap, self.actor = cdp, cap, actor
        self.tpls, self.cfg, self.log = tpls, cfg, log
        self._last_beat = 0.0
        # Every capture is both evidence the bot is alive AND our chance to read
        # the operator's buttons - see `on_capture`.
        self.cap.on_activity = self.on_capture
        self.controls, self.dock = controls, dock
        self.port = port
        self.mode = "paused"           # running | paused | stopped
        self.quit = False
        self.unknown = 0
        self.task = "resume_to_lobby"
        self.cycle = 0
        self.state = "-"
        self.note = ""
        self.t0 = time.time()
        self.max_unknown = 20
        # Unrecognised frames before trying a reload, and how many reloads to
        # allow before pausing for a human. Well below max_unknown, so the relog
        # gets its chance first, but not so low that a brief hiccup costs a
        # mission.
        m = cfg.get("mission", {})
        self.relog_after = int(m.get("relog_after_unknown", 8))
        self.max_relogs = int(m.get("max_relogs", 2))
        self._relogs = 0
        # Focus mode is armed by default and applied as soon as the game is
        # detected. It is not decoration: hiding the page chrome lets the game
        # reflow to the top so scrollY is 0 and STAYS 0, which is what makes the
        # bot's coordinates mean the same thing from one minute to the next.
        # Editable from the panel, and persisted OUTSIDE the tracked config so
        # experimenting with slots never dirties a versioned file.
        self.skills = _read_skills()
        f = _read_json(FARM_PATH, {})
        self.grade = f.get("grade")          # None == auto
        self.pin_page = f.get("page")        # None == highest unlocked
        self.pin_row = f.get("row")
        self.focus_wanted = True
        self.focus_on = False
        self.focus_aligned = False
        self.resumer = resume.Resumer(cap, actor, tpls, log, controls=controls)
        # Without this the dock deadlocks the bot: Pause parks a task inside
        # `Controls.wait_if_paused`, and the loop that reads the operator's next
        # button press is the one now parked. Pumping from inside the wait keeps
        # Run and Stop live even mid-mission.
        if controls is not None:
            controls.on_wait = lambda: self.pump(poll=0.0)

    # -- panel -------------------------------------------------------------
    def _uptime(self):
        s = int(time.time() - self.t0)
        return f"{s//3600}h {s%3600//60}m" if s >= 3600 else f"{s//60}m {s%60}s"

    def _refresh_no_click_zone(self):
        """Re-read where the panel actually is, every cycle.

        THE ZONE WENT STALE AND THE BOT PRESSED THE OPERATOR'S OWN BUTTONS. It
        was captured once at attach and never updated, so any layout shift left
        the guard defending empty space. Observed live: the panel moved, and bot
        clicks aimed at the game landed on the dock - setting a mission pin,
        toggling focus mode twice, and finally hitting RELOG, which reloaded the
        page and dropped the session back to character select. None of those were
        operator commands; the log recorded them as though they were.

        `dock_rect()` is a single JS evaluate, so re-reading it per cycle is far
        cheaper than one wrong click. The zone is REPLACED, never appended, or
        the list would grow without bound.
        """
        try:
            rect = self.dock.dock_rect()
        except Exception:
            return
        if not rect:
            return
        if getattr(self, "_zone", None) != rect:
            if getattr(self, "_zone", None) in self.actor.no_click_zones:
                self.actor.no_click_zones.remove(self._zone)
            if rect not in self.actor.no_click_zones:
                self.actor.no_click_zones.append(rect)
            if getattr(self, "_zone", None) is not None:
                self.log.info("dock moved; no-click zone is now %s", rect)
            self._zone = rect

    def push(self):
        self._refresh_no_click_zone()
        try:
            self.dock.render({
                "mode": self.mode, "state": self.state, "task": self.task,
                "cycle": self.cycle, "uptime": self._uptime(),
                "note": self.note, "tasks": TASKS, "log": self.log.lines[-10:],
                "focus": self.focus_on,
                "skills": self.skills, "skill_slots": SKILL_SLOTS,
                "grades": GRADES, "grade": self.grade,
                "pin": (f"page {self.pin_page} row {self.pin_row}"
                        if self.pin_page and self.pin_row else None),
            })
        except (OSError, CDPError) as e:
            raise Disconnected(str(e))
        except Exception as e:
            print(f"  (panel update failed: {type(e).__name__}: {e})", flush=True)

    # -- operator commands -------------------------------------------------
    def _apply(self, cmd):
        c = cmd.get("cmd")
        if c == "run":
            self.mode = "running"
            self.unknown = 0
            _write_control("run")
            self.log.info("operator: RUN (%s)", self.task)
        elif c == "pause":
            self.mode = "paused"
            _write_control("pause")
            self.log.info("operator: PAUSE")
        elif c == "stop":
            # STOP ENDS THE PROCESS, IMMEDIATELY, FROM WHEREVER WE ARE.
            #
            # A cooperative stop is not good enough here. `_apply` is reached
            # from `pump`, which is called from the capture hook and the gate's
            # poll loop - and BOTH of those wrap the call in `except Exception`,
            # so an exception raised here to unwind the stack is swallowed. The
            # flag-and-check alternative only takes effect at the next place
            # something bothers to look, which mid-mission can be a whole battle
            # away. Pressing Stop and watching the bot finish the fight is not
            # what Stop means.
            #
            # This used to be avoided because "the dock vanished with the
            # process". It no longer does: the panel is injected into the PAGE,
            # so it outlives us, and its staleness banner now says, accurately,
            # that no bot is attached and prints the command to relaunch. So the
            # operator is left with a visible, honest panel rather than a live
            # one that ignores them.
            self.log.info("operator: STOP - terminating the bot process")
            _write_control("stop")
            self.mode = "stopped"
            self._hard_exit()
        elif c == "skill":
            k = cmd.get("arg")
            if k in SKILL_SLOTS:
                self.skills.append(k)
                _write_skills(self.skills)
                self.log.info("operator: skill order -> %s",
                              " ".join(self.skills))
        elif c == "skill_clear":
            self.skills = []
            _write_skills(self.skills)
            self.log.info("operator: skill order cleared (Attack only)")
        elif c == "grade":
            g = cmd.get("arg")
            self.grade = None if g == "auto" else (g if g in GRADES else self.grade)
            self._save_farm()
            self.log.info("operator: grade -> %s", self.grade or "auto")
        elif c in ("page_up", "page_dn", "row_up", "row_dn"):
            # Stepping either one turns the pin ON; both default to 1 so the
            # first press pins page 1 row 1 rather than something arbitrary.
            self.pin_page = self.pin_page or 1
            self.pin_row = self.pin_row or 1
            if c == "page_up":
                self.pin_page += 1
            elif c == "page_dn":
                self.pin_page = max(1, self.pin_page - 1)
            elif c == "row_up":
                self.pin_row = min(3, self.pin_row + 1)
            else:
                self.pin_row = max(1, self.pin_row - 1)
            self._save_farm()
            self.log.info("operator: pinned mission -> page %d row %d",
                          self.pin_page, self.pin_row)
        elif c == "pin_off":
            self.pin_page = self.pin_row = None
            self._save_farm()
            self.log.info("operator: mission -> highest unlocked")
        elif c == "focus":
            self.focus_wanted = not self.focus_wanted
            self.dock.focus(self.focus_wanted)
            self.focus_on = self.focus_wanted
            self.log.info("operator: focus mode %s",
                          "ON" if self.focus_wanted else "off")
        elif c == "quit":
            # Same immediacy as Stop, and the distinction stays meaningful:
            #   Stop -> the bot dies, the PANEL STAYS (saying no bot is attached)
            #   Quit -> the bot dies and the panel is removed as well
            self.log.info("operator: QUIT - removing the panel and exiting")
            _write_control("stop")
            self.mode = "stopped"
            try:
                self.dock.remove()
            except Exception:
                pass
            self._hard_exit()
        elif c == "task":
            if any(t["key"] == cmd.get("arg") for t in TASKS):
                was, self.task = self.task, cmd["arg"]
                # SWITCHING TASK MUST INTERRUPT THE ONE IN FLIGHT.
                #
                # Setting the field alone was useless in practice: a mission
                # takes minutes and the cycle loop runs it to completion, so
                # pressing "TP training" mid-farm looked like nothing happened
                # at all - and the only way out was Stop, which now kills the
                # process, leaving the panel detached. That is the "tasks
                # shooting each other's foot" the operator hit.
                #
                # The abort uses the mechanism that already exists: the
                # file-backed stop switch every task honours at its own gates.
                # The task unwinds cleanly wherever it is, then the loop puts
                # the switch back to "run" and the NEW task starts. Nothing is
                # killed and the panel survives.
                if self.mode == "running" and was != self.task:
                    self._switching = True
                    _write_control("stop")
                    self.log.info("operator: task -> %s (interrupting %s)",
                                  self.task, was)
                else:
                    self.log.info("operator: task -> %s", self.task)
        elif c == "relog":
            self.log.info("operator: RELOG")
            self.relog()

    HEARTBEAT_EVERY = 3.0        # seconds; see the note below

    def _hard_exit(self, code=0):
        """Leave now, but leave things tidy.

        `os._exit` skips `finally`, so the pid lock has to be released HERE -
        forgetting it is what produces "another bot window is already running"
        on the next launch. Everything is best-effort: a stop must not be able
        to fail.
        """
        try:
            self.push()              # let the panel show 'stopped' first
        except Exception:
            pass
        try:
            self.cdp.close()
        except Exception:
            pass
        try:
            lock = os.path.join(ROOT, "run/app.lock")
            with open(lock) as f:
                mine = f.read().strip() == str(os.getpid())
            if mine:
                os.unlink(lock)
        except Exception:
            pass
        print("  stopped by the operator", flush=True)
        os._exit(code)

    def pump(self, poll=0.25):
        for cmd in self.dock.commands(poll=poll):
            self._apply(cmd)
        # KEEP THE PANEL'S "ALIVE" CLOCK TICKING DURING LONG WORK. `pump` is
        # called from the gate's poll loop, so it runs throughout a mission,
        # whereas `push` only runs between cycles - and a mission blocks for
        # minutes. Without this the panel's own watchdog showed "no bot attached
        # - the panel is frozen" for most of every mission, while the bot was
        # working perfectly. A full render is a large payload, so send only the
        # one-assignment heartbeat, and throttle it: the gate polls ~10x a
        # second and this is a CDP round trip.
        self.beat()

    def relog(self):
        """Reload the page to get back to a screen the ladder can read.

        RELOAD ONLY - this NEVER authenticates. CLAUDE.md is explicit that a
        logged-out session may mean a password change or a ban and must surface
        to the human. The resume ladder handles what comes back, clicking Play
        BY TEMPLATE at character select so `Delete`, which sits beside it, is
        never a candidate.

        It abandons whatever mission was in flight, which is the point: it is
        the only way out of a screen the ladder cannot name.
        """
        self.cdp.call("Page.reload")
        time.sleep(4.0)
        browser.pin_viewport(self.cdp, *VIEWPORT)
        try:
            from geometry import BattleGeometry
            BattleGeometry.forget()      # the layout may have moved
        except Exception:
            pass

    def on_capture(self):
        """Called on every screen capture. Reads buttons; keeps the panel alive.

        THE OPERATOR'S BUTTONS HAVE TO BE READ FROM HERE, not just from gates.
        Farm navigation - paging the mission list, opening a mission - never
        enters a gate, so nothing drained the command queue for the whole of it.
        Pressing "TP training" mid-farm therefore did nothing observable, and
        the only apparent escape was Stop, which kills the process and leaves
        the panel detached. Measured: the command was sent and no `task` event
        ever reached the log.

        Draining is cheap - `select` on a socket with nothing on it - so it runs
        on every capture. The heartbeat inside `pump` has its own throttle.

        Re-entrancy is guarded: `_apply` can itself capture (a relog, a render),
        and pumping from inside a pump would recurse.
        """
        if getattr(self, "_in_pump", False):
            return
        self._in_pump = True
        try:
            self.pump(poll=0.0)
        except Exception:
            pass
        finally:
            self._in_pump = False

    def beat(self):
        """Throttled 'still alive' ping for the panel.

        Wired to `Capture.on_activity`, so it fires from EVERY code path that
        looks at the screen - the resume ladder, farm navigation, gates,
        missions, minigames. Hooking the gate alone was not enough: the farm's
        own list navigation never enters a gate, so the panel sat stale through
        all of it and showed "no bot attached".

        Throttled because captures run many times a second and each beat is a
        CDP round trip.
        """
        now = time.time()
        if now - getattr(self, "_last_beat", 0.0) < self.HEARTBEAT_EVERY:
            return
        self._last_beat = now
        try:
            self.dock.heartbeat()
        except Exception:
            pass

    # -- work --------------------------------------------------------------
    def step(self):
        """One cycle of the selected task. Blocks for as long as it takes."""
        self.cycle += 1
        if self.task == "idle":
            self.state = "idle"
            return

        # ALREADY IN A MISSION? Then play it, and do not ask the resume ladder
        # to find the lobby first - it cannot, because battles and traversal are
        # deliberately not its job. Demanding the lobby before acting is why a
        # session that began mid-mission logged "no anchor matched" forever
        # while a battle sat waiting for input.
        if self.task == "farm_missions":
            try:
                import farm as farm_mod
                where = farm_mod.in_mission(self.cap.frame(gray=False), self.tpls)
            except (OSError, CDPError) as e:
                raise Disconnected(str(e))
            except Exception:
                where = None
            if where:
                self.state = where
                self.note = f"mission already in progress ({where}) - playing it"
                self.log.info("%s", self.note)
                self.push()
                self._run_mission()
                return
            # A traversal screen has no anchor of its own - it is scenery - so
            # the ladder cannot name it and the bot used to sit there while the
            # mission waited for it to walk. Hand over only after the ladder has
            # failed REPEATEDLY and nothing says we are in the village, because
            # what this licenses is a click on the map edge, and a map-edge click
            # in the village lands on a building.
            if self.unknown >= 3:
                try:
                    scene = farm_mod.looks_like_mission_scene(
                        self.cap.frame(gray=False), self.tpls)
                except Exception:
                    scene = False
                if scene:
                    self.state = "traversal"
                    self.note = ("no anchor anywhere and nothing says village - "
                                 "treating this as a mission map and walking")
                    self.log.info("%s", self.note)
                    self.push()
                    self.unknown = 0
                    self._run_mission()
                    return

        gray = cv2.cvtColor(self.cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
        out, info = self.resumer.advance(gray)
        self.state = info.get("step", out)
        if out == resume.HALT:
            # A halt is a human's problem, not something to retry. Signing out
            # is the common one and the bot must never resolve it itself.
            self.note = info.get("reason") or f"halted at {self.state}"
            self.log.error("HALT: %s", self.note)
            self.mode = "paused"
            return
        if self.state == "unknown":
            self.state = self._name_screen() or "unknown"

        if out != resume.ARRIVED:
            # BOUND THE UNKNOWN STREAK. `Resumer.run()` has a `max_unknown`
            # guard; `advance()` does not, and this loop calls `advance()`. Live,
            # that meant 52 consecutive "no anchor matched" cycles on a screen
            # the ladder could not read - a bot spinning silently is worse than
            # one that stops and says why.
            if self.state in ("unknown",) or out == "unknown":
                self.unknown += 1
            else:
                self.unknown = 0
            self.note = f"resume: {out} ({self.state})"
            # TRY A RELOG BEFORE GIVING UP.
            #
            # The ladder deliberately cannot name a battle or a traversal
            # screen, so a task that needs the LOBBY can never start from inside
            # a mission - switching to TP training mid-farm just accumulated
            # unrecognised frames until the bot paused. A reload lands on
            # character select, which the ladder does know, and it walks itself
            # back to the lobby from there.
            #
            # It is deliberately NOT the first response: a relog throws away an
            # in-flight mission, so it only fires once the state has been
            # unreadable for a while, and at most `max_relogs` times per streak
            # so a screen that survives a reload cannot loop forever.
            if (self.unknown >= self.relog_after
                    and self._relogs < self.max_relogs):
                self._relogs += 1
                self.note = (f"state unreadable for {self.unknown} frames - "
                             f"relogging to get back to the lobby "
                             f"({self._relogs}/{self.max_relogs})")
                self.log.info("%s", self.note)
                self.unknown = 0
                try:
                    self.relog()
                except (OSError, CDPError) as e:
                    raise Disconnected(str(e))
                return
            if self.unknown >= self.max_unknown:
                self.note = (f"{self.unknown} unrecognised frames in a row - "
                             f"pausing rather than spinning. Look at the screen.")
                self.log.error("%s", self.note)
                self.mode = "paused"
                self.unknown = 0
                self._relogs = 0
            return
        self.unknown = 0
        # The ladder got somewhere it recognises, so the relog budget is spent
        # on a DIFFERENT problem next time rather than staying exhausted.
        self._relogs = 0
        self.note = ""

        if self.task == "resume_to_lobby":
            self.mode = "paused"
            self.log.info("arrived in the lobby; pausing")
            return

        if self.task == "tp_training":
            import tp as tp_mod
            self.note = "TP run in flight - the panel pauses until it finishes"
            self.push()
            # Play whatever is listed, identifying each minigame from the
            # screen. Names are not used to choose: the family a title implies
            # is not guaranteed to be the minigame you get, and a name-matched
            # picker silently skips anything renamed or newly added.
            played, banked = tp_mod.run_all(self.cap, self.actor, self.log)
            self.note = f"TP pass: {played} started, {banked} banked"
            self.log.info(self.note)
            self.mode = "paused"
            return

        if self.task == "farm_missions":
            # Choose by READING the grade panel rather than by config: the grade
            # bars are colour coded and a locked grade renders grey, so "best
            # available" is a measurement. See engine/farm.py.
            import farm as farm_mod
            self.note = "mission in flight - the panel pauses until it finishes"
            self.push()
            started, banked = farm_mod.farm(self.cap, self.actor, self.log,
                                            self.battle_cfg(), self.controls,
                                            repeat=1)
            self.note = f"farm: {started} started, {banked} banked"
            self.log.info(self.note)

    def _save_farm(self):
        _write_json(FARM_PATH, {"grade": self.grade, "page": self.pin_page,
                                "row": self.pin_row})

    def battle_cfg(self):
        """The config a mission should run with, including the panel's skills.

        The rotation is applied HERE rather than written into the config file, so
        what the operator picked in the panel is what the very next battle uses -
        that is the whole point of it being editable live. An empty order means
        Attack only, which is the documented safe default.
        """
        cfg = dict(self.cfg)
        if self.skills:
            b = dict(cfg.get("battle", {}))
            b["rotation"] = list(self.skills)
            cfg["battle"] = b
        m = dict(cfg.get("mission", {}))
        m["grade"] = self.grade
        m["mission_page"], m["mission_row"] = self.pin_page, self.pin_row
        cfg["mission"] = m
        return cfg

    def _run_mission(self):
        """Play a mission that is already under way."""
        import mission as mission_mod
        from gate import Gate
        cfg = self.battle_cfg()
        if self.skills:
            self.log.info("using the panel's skill order: %s",
                          " ".join(self.skills))
        r = mission_mod.MissionRunner(
            Gate(self.cap, self.log, self.controls), self.actor, self.cap,
            self.tpls, cfg, self.log, self.controls)
        r.grade = self.cfg.get("mission", {}).get("grade") or "A"
        try:
            out, stats = r.run()
            self.note = f"mission: {out} {stats}"
            self.log.info("%s", self.note)
        except Exception as e:
            self.note = f"mission runner: {type(e).__name__}: {e}"
            self.log.error("%s", self.note)
            self.mode = "paused"

    def browser_alive(self, timeout=2.0):
        """Is the BROWSER still there? (not: is our page still there)

        A closed window and a navigated page look identical at the socket - both
        just kill the connection - but they need opposite responses: reconnect
        to a navigation, exit on a closed window. The CDP HTTP endpoint tells
        them apart, because it dies with the browser process.
        """
        import urllib.request
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/version", timeout=timeout)
            return True
        except Exception:
            return False

    def reconnect(self):
        """Rebuild the connection after the socket dies.

        LOGGING OUT KILLS THE SOCKET. The page navigates, Chrome tears the target
        down, and every later call raises BrokenPipeError. Without this the
        process stayed alive doing nothing but logging "panel update failed"
        forever, and the only way to get the panel back was to restart by hand —
        which is exactly the thing the dock is supposed to save you from.
        """
        self.log.info("connection lost - reconnecting")
        gone = 0
        for attempt in range(30):
            # CHECK THE BROWSER BEFORE PAYING FOR AN ATTACH. `attach` blocks up
            # to 30 s per try waiting for a page target, so a closed window used
            # to cost 30 x 32 s of pointless retrying while HOLDING THE PID LOCK
            # - the operator closes the window, tries to relaunch, and is told
            # "another bot window is already running". Measured: one such
            # process was still alive, and wedged, SEVEN HOURS later.
            if not self.browser_alive():
                gone += 1
                if gone >= 3:
                    self.log.info("the browser window is gone - shutting down "
                                  "so the next launch is not blocked")
                    self.quit = True
                    return False
                time.sleep(2.0)
                continue
            gone = 0
            try:
                c, cap, actor, dk = attach(self.port, self.log, self.tpls)
            except Exception as e:
                if attempt % 5 == 0:
                    self.log.info("  reconnect attempt %d failed (%s)",
                                  attempt + 1, type(e).__name__)
                time.sleep(2.0)
                continue
            try:
                self.cdp.close()
            except Exception:
                pass
            self.cdp, self.cap, self.actor, self.dock = c, cap, actor, dk
            # The Capture object is NEW after a reconnect, so the activity hook
            # has to be re-installed or the panel goes stale from here on and
            # the operator's buttons stop being read.
            self.cap.on_activity = self.on_capture
            self.resumer = resume.Resumer(cap, actor, self.tpls, self.log,
                                          controls=self.controls)
            self.log.info("reconnected; the panel is back")
            return True
        self.log.error("could not reconnect after 30 attempts")
        return False

    def _name_screen(self):
        """A conservative label for a screen the resume ladder cannot name.

        DO NOT use `minigame.classify` here. That function is for deciding what
        to PLAY once a TP mission is already open, where the only possibilities
        are its three minigames or a battle. Pointed at arbitrary screens it
        false-positives: on the character-select screen it reports "kekkai",
        because its seal search is a dark-red blob search and CLAUDE.md already
        records that the same search hits village architecture and the
        character's own red robe. A label is not worth a wrong answer - the
        operator reads it and believes it.

        So this uses only HIGH-MARGIN anchors, and stays silent otherwise:

          * combat     two corroborating command buttons via BattleGeometry,
                       the same gate MissionRunner uses. On character select it
                       correctly declines: charge 0.370, dodge 0.353, locate None.
          * the TP minigame HUDs, which are single templates measuring 1.000
            against 0.27..0.36 everywhere else.

        Anything else stays "unknown", which is an honest answer.
        """
        try:
            frame = self.cap.frame(gray=False)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        except (OSError, CDPError) as e:
            raise Disconnected(str(e))
        except Exception:
            return None

        from perceive import find
        for name, label in (("tp_seal_hud", "hand-seal minigame"),
                            ("tp_cards_hud", "memory board")):
            t = self.tpls.get(name)
            if t is not None and find(gray, t)[0].found:
                return label

        ch, do = self.tpls.get("charge_btn"), self.tpls.get("dodge_btn")
        if ch is not None and do is not None:
            try:
                from geometry import BattleGeometry
                if BattleGeometry.locate(gray, ch, do) is not None:
                    return "combat"
            except Exception:
                pass
        return None

    def ensure_focus(self):
        """Apply focus mode once the game is on the page.

        Waits for the game rather than doing it on load: before sign-in there is
        no game to focus on, and hiding the login page would leave the operator
        staring at nothing.
        """
        if not self.focus_wanted:
            return
        # ASK THE PAGE, DO NOT TRUST THE CACHED FLAG. `focus_on` is a Python-side
        # belief and a reload silently invalidates it: the panel re-injects
        # itself on the new document (addScriptToEvaluateOnNewDocument), so the
        # presence check `ensure_dock` does still passes, while the fresh
        # document is NOT focused. `focus_on` stayed True from before the reload
        # and the early return below meant focus was never re-applied - measured
        # after a Relog: `__nsbotFocusOn` false and `scrollY` 301, i.e. the game
        # had drifted out of the viewport exactly as this file warns.
        #
        # Reading the real state is one cheap evaluate and it also keeps the
        # convergence property that matters: focus is re-applied only when the
        # PAGE says it is off, never merely because we re-injected, which is what
        # previously made the game jump around and the state read "unknown".
        try:
            live = self.dock.focus_state()
        except (OSError, CDPError) as e:
            raise Disconnected(str(e))
        except Exception:
            live = self.focus_on
        if live != self.focus_on:
            # The document changed under us; the one-shot align owes us a pass.
            self.focus_aligned = False
        self.focus_on = bool(live)

        if self.focus_on:
            # RE-ALIGN EVERY CYCLE, not once.
            #
            # A one-shot align cannot hold: the page scrolls and the layout
            # reflows later, and nothing put the game back. Measured while the
            # card board was on screen - `scrollY` 60 and the game iframe at
            # y = -118 CSS, i.e. **-236 captured px**, which is exactly the
            # -237 by which the board's rows had moved. Every minigame's
            # geometry is absolute, so the whole lot misses by that amount and
            # each one fails in its own confusing way: the memory board "gone",
            # the kekkai runes un-clickable, the Special tab "not found".
            #
            # This cannot cause the jumping that re-APPLYING focus used to,
            # because `align` is a no-op when the game is already in place - it
            # returns "aligned" and touches nothing.
            try:
                r = self.dock.align()
                if r == "realigned":
                    self.log.info("focus: the game had drifted; re-aligned")
                self.focus_aligned = True
            except Exception:
                pass
            return
        try:
            if not self.dock.game_ready():
                return
            r = self.dock.focus(True)
            if r in ("focused", "already"):
                self.focus_on = True
                self.focus_aligned = False
                self.log.info("focus mode on - page chrome hidden, game pinned "
                              "to the top (scroll is now deterministic)")
        except (OSError, CDPError) as e:
            raise Disconnected(str(e))
        except Exception:
            pass

    def ensure_dock(self):
        """Keep the panel on screen no matter what the page does.

        `Page.addScriptToEvaluateOnNewDocument` covers navigations within this
        target, but not everything: signing out, a target swap, or the script
        registration being lost all leave the operator with no controls and no
        way to get them back short of restarting. A presence check per cycle is
        one cheap `Runtime.evaluate`, so just do it.
        """
        try:
            here = self.cdp.evaluate(
                f"(!!document.getElementById({dock_mod.PANEL_ID!r}))")
        except (OSError, CDPError) as e:
            raise Disconnected(str(e))
        except Exception:
            return False
        if here:
            return True
        self.log.info("dock missing (navigation?) - re-injecting")
        try:
            self.dock.install(verify=False)
            # A fresh document really is unfocused, but ASK rather than assume -
            # assuming forced a re-apply on every re-injection, and re-applying
            # is what made the game jump around and the state read "unknown".
            self.focus_on = self.dock.focus_state()
            return True
        except Exception as e:
            self.log.error("could not re-inject the dock: %s", e)
            return False

    def loop(self, tick=1.0):
        self.log.info("ready - press Run in the panel")
        self.push()
        idle_checks = 0
        while not self.quit:
            try:
                # THE WINDOW CAN BE CLOSED WHILE WE ARE IDLE, and then nothing
                # raises: no call is in flight, so no socket error surfaces and
                # the process sits there holding the pid lock. Poll the browser
                # itself now and then - it is one local HTTP request.
                idle_checks += 1
                if idle_checks % 5 == 0 and not self.browser_alive():
                    self.log.info("the browser window was closed - exiting "
                                  "(the next launch would otherwise be refused "
                                  "with 'another bot window is already running')")
                    self.quit = True
                    break
                self.ensure_dock()
                self.ensure_focus()
                self.pump()
                if self.mode == "running":
                    try:
                        try:
                            self.step()
                        finally:
                            # RE-ARM IN `finally`. The stop switch was thrown to
                            # interrupt the previous task; if `step` raises on
                            # the way out, leaving it thrown would wedge the bot
                            # in a permanent stop that no button explains.
                            if getattr(self, "_switching", False):
                                self._switching = False
                                _write_control("run")
                                self.log.info("switched to %s", self.task)
                    except Disconnected:
                        raise
                    except (OSError, CDPError) as e:
                        raise Disconnected(str(e))
                    except Exception as e:
                        self.note = f"{type(e).__name__}: {e}"
                        self.log.error("task error: %s", self.note)
                        self.mode = "paused"
                self.push()
            except Disconnected as e:
                self.log.info("disconnected (%s)", e)
                self.mode = "paused"
                if not self.reconnect():
                    break
                continue
            if self.mode != "running":
                time.sleep(tick)
        # Best-effort final render: if we are here because the browser died,
        # this cannot work and must not raise out of loop() - the caller's
        # `finally` is what releases the pid lock.
        try:
            self.push()
        except Exception:
            pass
        self.log.info("quit")


def _read_json(rel, default):
    try:
        with open(os.path.join(ROOT, rel)) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(rel, value):
    p = os.path.join(ROOT, rel)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(value, f)
    except Exception:
        pass


def _read_skills():
    return [k for k in _read_json(SKILLS_PATH, []) if k in SKILL_SLOTS]


def _write_skills(order):
    _write_json(SKILLS_PATH, order)


def _write_control(value):
    p = os.path.join(ROOT, "run/bot.control")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(value)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--config", default=os.path.join(ROOT, "Configs/mission.json"))
    ap.add_argument("--attach", action="store_true",
                    help="use a browser that is already running on --port "
                         "instead of launching an app window")
    ap.add_argument("--no-dock", action="store_true",
                    help="skip the panel (headless-ish; controls via run/bot.control)")
    a = ap.parse_args()

    # ONE RUNNER AT A TIME. Nothing stopped a second instance attaching to the
    # same tab, and instances stack silently: eight of them were found running
    # together, each pushing its own state into the shared panel every second and
    # each CLICKING THE GAME. From the outside that looks like the panel toggling
    # at random and the bot fighting itself - which is exactly what it was.
    lock = os.path.join(ROOT, "run/app.lock")
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    try:
        with open(lock) as f:
            other = int((f.read() or "0").strip() or 0)
        os.kill(other, 0)                     # raises unless it is alive
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        other = None
    except Exception:
        other = None
    if other and other != os.getpid():
        print(f"  another bot window is already running (pid {other}). "
              f"Close it first, or: kill {other}", flush=True)
        return 3
    with open(lock, "w") as f:
        f.write(str(os.getpid()))

    cfg = json.load(open(a.config))
    url = cfg.get("target", {}).get("game_url", "")
    profile = os.path.join(ROOT, "run/chrome-profile")

    if not a.attach:
        # app_mode gives a window with no tab strip and no omnibox - the whole
        # "native app" part of this is one Chrome flag. Measured: window chrome
        # drops from 274 px to 90 px, i.e. tab strip + omnibox + bookmarks gone.
        #
        # `launch` REUSES a browser that is already serving CDP, and that matters
        # more than it looks: **the site's session cookie is a browser-session
        # cookie, so quitting Chrome signs you out.** Measured - closing the
        # browser and relaunching it landed on the logged-out page with only
        # `_ga` and `cf_clearance` left. So never restart the browser to "get a
        # clean window"; attach to the one that is already open.
        browser.launch(url, profile, port=a.port, app_mode=True,
                       window=(VIEWPORT[0] + 8, VIEWPORT[1] + 90))

    log = Log()
    tpls = load_templates(cfg, log)
    controls = Controls(os.path.join(ROOT, "run/bot.control"), log)
    _write_control("pause")

    c, cap, actor, dock = attach(a.port, log, tpls,
                                 install_dock=not a.no_dock)
    if not a.no_dock:
        geo = dock.geometry()
        log.info("game %s, dock %s", geo.get("game"), geo.get("dock"))
        log.info("dock is a no-click zone for the bot: %s", dock.dock_rect())

    r = Runner(c, cap, actor, tpls, cfg, log, controls, dock, port=a.port)
    try:
        r.loop()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        c.close()
        try:
            with open(lock) as f:
                if f.read().strip() == str(os.getpid()):
                    os.unlink(lock)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
