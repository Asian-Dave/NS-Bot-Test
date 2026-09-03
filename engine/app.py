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
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2

import browser
import dock as dock_mod
import presence
import resume
import tasks
from act import Actor, Controls
from perceive import load_templates
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

# Selectable window geometries.
#
# MEASURED, and the two axes behave completely differently:
#   * viewport WIDTH/HEIGHT do not resize the game at all - it stays 960x839 CSS
#     and merely RE-CENTRES (x went 380 -> 240 -> 480 -> 160 across
#     1720/1440/1920/1280). `Capture.fix` absorbs that as pure offset.
#   * DPR keeps it 960x839 CSS but scales the captured pixels - canvas width
#     960 / 1920 / 2880 at dpr 1 / 2 / 3 - which scales EVERY measured constant.
#
# So there is no table of client window sizes to support: the game is one fixed
# 960-wide stage (its AIR manifest is resizable/fullScreen around
# <width>960</width>) and every size is a uniform transform of it.
#
# dpr 2 is the reference every template was cut at. The others are offered
# because they work through the transform, but templates are NOT re-cut for
# them, so matching is weaker off-reference - which is why the panel warns.
# MINIMUM WIDTH IS A CONSTRAINT, NOT A PREFERENCE. The page centres the game in
# the FULL viewport, ignoring the panel, so with a 960-wide game and a 380-wide
# panel:
#
#     centred game   (W+960)/2 <= W-380   ->   W >= 1720
#
# Measured the hard way: a 1440 viewport put the game at 240..1200 against a
# dock starting at 1060 - a 140 px OVERLAP, with the panel drawn on top of the
# game and the no-click zone covering playable area. NOTHING BELOW 1720 IS SAFE
# while the game is centred.
#
# Flush-lefting the game would drop the floor to 1340, and that was tried and
# REVERTED - see the note in dock.py's focus(). Do not re-add a narrower option
# here without that working first.
MIN_VIEWPORT_W = 1720
VIEWPORTS = [
    {"key": "1720x720@2", "label": "1720x720", "w": 1720, "h": 720, "dpr": 2},
    {"key": "1920x900@2", "label": "1920x900", "w": 1920, "h": 900, "dpr": 2},
    {"key": "2200x980@2", "label": "2200x980", "w": 2200, "h": 980, "dpr": 2},
    {"key": "2560x1080@2", "label": "2560x1080", "w": 2560, "h": 1080, "dpr": 2},
]
VIEWPORT_PATH = "run/viewport.json"

# The panel's task list comes from the registry, so a task is declared in
# exactly one place - see engine/tasks.py. It used to be declared here AND
# handled in two separate branches of `step`.
TASKS = tasks.AS_DICTS


# OPENCV THREADS. Capped where the build allows it - which is NOT here.
#
# `cv2` defaults to one thread per core (18 on this machine) and every template
# match saturates them, which is most of "why does my Mac get hot while the bot
# runs". So capping looked like free heat relief.
#
# **A MEASUREMENT THAT WAS NOT MEASURING ANYTHING.** A sweep of thread counts
# came back suspiciously flat - 1.265 s at "1 thread" against 1.281 s at
# "18" - and the reason is not that threading fails to help. It is that this
# wheel is built with **GCD** as its parallel framework
# (`cv2.getBuildInformation()` -> "Parallel framework: GCD"), and under GCD
# `setNumThreads` is a NO-OP:
#
#     setNumThreads(4) -> getNumThreads() == 18
#     setNumThreads(1) -> getNumThreads() == 18
#
# So all seven rows measured the same configuration. The honest conclusion is
# that OpenCV's thread count is NOT controllable from Python on this build, and
# nothing here can reduce the core burn directly.
#
# The call is KEPT anyway, because it is not a no-op everywhere: Linux and
# Windows wheels use TBB / pthreads / OpenMP, where it does what it says. It is
# inert on this host rather than wrong, and it logs which case applies so the
# next person does not repeat the mistake above.
#
# WHAT ACTUALLY REDUCES THE HEAT IS DOING LESS WORK. The search band and the
# half-resolution prefilter cut template matching by 7.55x, and that is 7.55x
# less CPU burned - a real reduction, unlike this.
def _cap_cv_threads(log=None):
    try:
        cores = os.cpu_count() or 4
        want = max(1, min(4, cores // 2))
        had = cv2.getNumThreads()
        cv2.setNumThreads(want)
        got = cv2.getNumThreads()
        if log is not None:
            if got == had and had > want:
                log.info("opencv threads stay at %d - this build's parallel "
                         "framework ignores setNumThreads, so the heat can "
                         "only come down by doing less work", had)
            elif got != had:
                log.info("opencv threads %d -> %d (of %d cores)",
                         had, got, cores)
        return want
    except Exception:
        return None


class Log:
    """Logs to the terminal AND to the dock's log pane.

    THE STREAM IS TIMESTAMPED TOO, not just the dock. It used to stamp only the
    lines kept for the panel, so `run/app.log` had no times in it at all - and
    an operator pasting from the dock's pane got timestamps while the file that
    records a whole session did not. That makes the file useless for the one
    thing a log is for: working out where the seconds went. Every timing
    conclusion had to come from durations the code happened to print itself.

    Levels are distinguished for the same reason. `warning = error = info` meant
    a crash and a routine step looked identical in the file, so the eye had
    nothing to catch on when scanning a few thousand lines.
    """

    def __init__(self, sink=None, keep=14):
        self.lines, self.keep, self.sink = [], keep, sink

    def _emit(self, tag, m, a):
        msg = (m % a) if a else m
        stamp = time.strftime("%H:%M:%S")
        print(f"{stamp} {tag}{msg}", flush=True)
        # The dock pane is narrow, so it keeps the compact form it always had.
        self.lines.append(f"{stamp} {msg}"[:120])
        del self.lines[:-self.keep]

    def info(self, m, *a):
        self._emit("  ", m, a)

    def warning(self, m, *a):
        self._emit("  WARN  ", m, a)

    def error(self, m, *a):
        self._emit("  ERROR ", m, a)


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
    # Honour the operator's chosen window, so a restart does not silently snap
    # back to the reference size after they picked another.
    _vp = next((v for v in VIEWPORTS
                if v["key"] == _read_json(VIEWPORT_PATH, {}).get("key")), None)
    browser.pin_viewport(c, _vp["w"], _vp["h"], _vp["dpr"]) if _vp else \
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
        self.viewport = _read_json(VIEWPORT_PATH, {}).get(
            "key", VIEWPORTS[0]["key"])
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
        # Save a frame for teaching once the streak is clearly not a hiccup, but
        # BEFORE the relog wipes the screen away.
        self.teach_at = max(2, int(m.get("teach_at_unknown", 4)))
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
                "viewports": VIEWPORTS, "viewport": self.viewport,
                "viewport_label": next(
                    (v["label"] for v in VIEWPORTS if v["key"] == self.viewport),
                    self.viewport or ""),
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
            # STOP ABORTS THE TASK AT ONCE, AND KEEPS THE PANEL ALIVE.
            #
            # The original complaint was that Stop was QUEUED - pressed
            # mid-mission, it did nothing until the mission ended. That was real,
            # but the cause was not the mechanism: operator commands simply were
            # not being READ during long work, because `pump` ran only from the
            # gate's poll loop and farm navigation never enters a gate. Now that
            # `Capture.on_activity` pumps on every capture, the file-backed stop
            # switch is seen within a capture or two and the task unwinds at its
            # next gate.
            #
            # So killing the process is no longer needed to be immediate - and
            # killing it has a real cost: the panel is injected into the page, so
            # it survives, but its buttons have no receiver. The operator is left
            # with a dead panel and no way back except the terminal, which is
            # exactly the "no bot attached" they kept hitting. Quit still exits.
            # STOP = KILL AND RESET, at the operator's request.
            #
            # This has been both ways in this project, so the reasoning is
            # recorded. It first aborted the task and kept the process, because
            # killing it left a live-looking panel with dead buttons. That is
            # now a solved problem - the panel's staleness banner says "no bot
            # attached" and prints the relaunch command - and the operator wants
            # Stop to mean START AGAIN FROM NOTHING, not "pause here".
            #
            # So it wipes the transient task state as well. Everything held in
            # memory - the cycle counter, the unknown streak, the relog budget,
            # cached battle geometry, remembered dud targets - dies with the
            # process, which is most of the reset. What has to be cleaned up
            # explicitly is anything PERSISTED that would otherwise carry into
            # the next launch.
            self.log.info("operator: STOP - killing the bot and clearing task "
                          "progress")
            _write_control("stop")
            self.mode = "stopped"
            self._reset_task_state()
            self._respawn()
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
            # Quit is the one that EXITS. Stop aborts the task and leaves the
            # panel live so Run works again without a terminal.
            #   Stop -> the task stops, the process and panel stay
            #   Quit -> the panel is removed and the process exits
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
        elif c == "viewport":
            vp = next((v for v in VIEWPORTS if v["key"] == cmd.get("arg")), None)
            if vp is None:
                return
            # The dock asks for confirmation before sending this, because
            # applying it reloads the game and drops us to character select.
            self.log.info("operator: window -> %s (reloading)", vp["label"])
            self.viewport = vp["key"]
            _write_json(VIEWPORT_PATH, {"key": vp["key"]})
            self.mode = "stopped"
            _write_control("stop")
            try:
                browser.pin_viewport(self.cdp, vp["w"], vp["h"], vp["dpr"])
                time.sleep(0.6)
                self.relog()
                # Geometry hints and the drift cache are all keyed to the old
                # layout; keeping them would aim every click at the old place.
                self.cap._off_at = 0.0
                self.focus_aligned = False
            except (OSError, CDPError) as e:
                raise Disconnected(str(e))
            except Exception as e:
                self.log.error("could not apply the window size: %s", e)
        elif c == "relog":
            self.log.info("operator: RELOG")
            self.relog()

    HEARTBEAT_EVERY = 3.0        # seconds; see the note below

    # Files under run/ that hold TASK PROGRESS rather than operator PREFERENCES.
    # Preferences must survive a Stop - the grade, the pinned mission, the skill
    # order and the window size are all things the operator chose deliberately,
    # and wiping them would be a nasty surprise.
    PROGRESS_FILES = ("run/tasks.json",)

    def _reset_task_state(self):
        """Clear progress so the next launch starts fresh. Never preferences."""
        self.cycle = 0
        self.unknown = 0
        self._relogs = 0
        self.note = ""
        try:
            from geometry import BattleGeometry
            BattleGeometry.forget()
        except Exception:
            pass
        for rel in self.PROGRESS_FILES:
            try:
                os.unlink(os.path.join(ROOT, rel))
                self.log.info("cleared %s", rel)
            except FileNotFoundError:
                pass
            except Exception:
                pass

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
            if _lock_holder(lock) == os.getpid():
                os.unlink(lock)
        except Exception:
            pass
        print("  stopped by the operator", flush=True)
        os._exit(code)

    def _respawn(self):
        """Stop, reset, and come straight back attached. Does not return.

        WHY STOP RESTARTS RATHER THAN JUST DYING. Stop means "start again from
        nothing" here - the operator asked for that explicitly, and killing the
        process is most of the reset for free, since the cycle counter, the
        unknown streak, the relog budget, cached battle geometry and remembered
        dud targets all live in memory and die with it.

        But the PANEL is injected into the page, so it survives the process it
        was talking to. The result was a live-looking panel whose buttons had no
        receiver: it read "no bot attached", printed a relaunch command, and the
        operator had to go to a terminal every single time. A reset that costs a
        manual relaunch is not a reset, it is a chore.

        So the replacement is launched BEFORE we go, and it attaches to the
        Chrome that is already running - which matters more than it looks,
        because the game's session cookie is a browser-session cookie and
        relaunching the browser would cost a manual sign-in that only a human
        can do.

        ORDER IS THE WHOLE TRICK, and getting it wrong means two bots clicking
        one game:

          1. release the pid lock, or the replacement is refused by the guard
             that exists to prevent exactly the duplicate we are creating;
          2. hand the child OUR pid, so it waits for us to actually be gone
             before it touches anything;
          3. exit.

        The child's wait is what makes this safe, rather than a sleep chosen by
        guesswork on either side.
        """
        try:
            self.push()              # let the panel show 'stopped' first
        except Exception:
            pass
        try:
            self.cdp.close()
        except Exception:
            pass

        argv = [sys.executable, os.path.join(ROOT, "engine/app.py")]
        # Carry the original invocation forward, minus anything we must
        # override. `--attach` is forced because the browser is already up.
        skip_next = False
        for a in sys.argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if a == "--wait-for-pid":
                skip_next = True
                continue
            if a == "--attach":
                continue
            argv.append(a)
        argv += ["--attach", "--wait-for-pid", str(os.getpid())]

        # Release the lock BEFORE spawning - see the docstring.
        try:
            lock = os.path.join(ROOT, "run/app.lock")
            if _lock_holder(lock) == os.getpid():
                os.unlink(lock)
        except Exception:
            pass

        started = False
        try:
            log_path = os.path.join(ROOT, "run/app.log")
            # APPEND, so the restart does not erase the record of whatever the
            # operator pressed Stop about.
            fh = open(log_path, "a")
            kwargs = {"stdout": fh, "stderr": subprocess.STDOUT,
                      "stdin": subprocess.DEVNULL, "cwd": ROOT}
            if hasattr(os, "setsid"):
                kwargs["start_new_session"] = True      # POSIX: outlive us
            else:                                        # Windows
                kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen(argv, **kwargs)
            started = True
        except Exception as e:
            print(f"  could not relaunch: {e}", flush=True)

        if started:
            print("  stopped and cleared - relaunching attached "
                  "(the panel comes back on its own)", flush=True)
        else:
            print("  stopped by the operator - relaunch failed, so run: "
                  ".venv/bin/python engine/app.py --attach", flush=True)
        os._exit(0)

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
        # The reload re-injected the panel with no state and dropped focus.
        # Restore both here rather than leaving it to the next cycle, which a
        # running task may not reach for minutes.
        try:
            self.push()
            self._refocus_after_reload()
        except Exception:
            pass
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
        # HOLD THE ALIGNMENT DURING LONG TASKS TOO. `ensure_focus` only runs
        # between cycles, and a mission blocks for minutes - so the game could
        # drift mid-task and stay drifted, which is exactly how the kekkai runes
        # ended up located at a shifted position while the click never landed.
        # `align` is a no-op when the game is already in place, so this cannot
        # reintroduce the jumping that re-APPLYING focus caused.
        try:
            if self.focus_on and self.dock.align() == "realigned":
                self.log.info("focus: drifted mid-task; re-aligned")
        except Exception:
            pass
        try:
            if self.dock.heartbeat() == "empty":
                # The page reloaded and re-injected the panel with no state, so
                # it is showing a bare skeleton - no task buttons, no values.
                # Re-render NOW rather than at the end of the task, which during
                # a mission can be minutes away.
                self.log.info("panel re-injected empty (reload?) - re-rendering")
                self.push()
                # A reload also drops focus mode, and `ensure_focus` only runs
                # between cycles - so restore it here too or the game stays
                # unfocused for the rest of the task.
                self._refocus_after_reload()
        except Exception:
            pass

    def _save_for_teaching(self):
        """Write the current frame to ref/auto/unknown/ and flag it loudly.

        A screen the bot cannot name is not a mystery to be pondered - it is one
        template away from being handled. Every such case this project has hit
        (the mission list, a battle between turns, the seal-broken dialog, the
        Level Up panel) was fixed by cutting a single anchor from a single
        frame. The hard part was always CATCHING the frame; by the time an
        operator looks, the screen has moved on.
        """
        try:
            import time as _t
            d = os.path.join(ROOT, "ref/auto/unknown")
            os.makedirs(d, exist_ok=True)
            name = _t.strftime("unknown_%Y%m%d_%H%M%S.png")
            path = os.path.join(d, name)
            frame = self.cap.frame(gray=False)
            cv2.imwrite(path, frame)
            self.note = (f"UNRECOGNISED SCREEN - saved {name} for teaching. "
                         f"Cut an anchor from it and add a rung.")
            self.log.error("%s", self.note)
            self.log.error("  teach it: score templates against "
                           "ref/auto/unknown/%s, cut what is unique to this "
                           "screen, and add it to the ladder or the veto set",
                           name)
        except Exception as e:
            self.log.info("could not save the unrecognised frame: %s", e)

    def _refocus_after_reload(self):
        """Re-apply focus after the page reloaded under a running task."""
        if not self.focus_wanted:
            return
        try:
            if self.dock.focus_state():
                return
            if not self.dock.game_ready():
                return
            if self.dock.focus(True) in ("focused", "already"):
                self.focus_on = True
                self.focus_aligned = False
                self.log.info("focus mode restored after the reload")
        except Exception:
            pass

    # -- work --------------------------------------------------------------
    # HOW MANY SETBACKS A LOOPING TASK RIDES OUT before it hands back to a
    # human. Generous on purpose: the game's own render-stall bug (a mission
    # that draws the map but never the character) is cleared by a relog, and
    # "farm until I press Stop" means surviving that without supervision.
    MAX_SETBACKS = 6

    def _setback(self, note, fatal=False):
        """Something went wrong. Carry on, or hand back to a human?

        WHY THIS IS ONE DECISION AND NOT FOUR. Four separate paths used to
        pause the bot - an unreadable screen, a task error, a mission-runner
        error, and a disconnect - and each did it unconditionally. For a
        ONE-SHOT that is right: it was asked to do a thing once, and the thing
        failed. For a LOOPING task it is wrong, because "farm missions" means
        keep farming, and pausing on the first hiccup left the bot sitting idle
        until someone noticed and pressed Run.

        The distinction is whether a HUMAN is required, not how alarming the
        message looks:

            logged out / HALT   -> fatal. Only a person can sign in, and this
                                   bot must never try.
            unreadable screen   -> a relog usually clears it (this is the
                                   documented recovery for the game's render
                                   stall), so relog and carry on.
            task error          -> may be transient; retry a bounded number of
                                   times before giving up.

        Bounded, because a bot spinning silently is worse than one that stops
        and says why - this file's own rule. The budget resets whenever a cycle
        gets somewhere recognisable, so it is spent on the NEXT problem rather
        than staying exhausted.
        """
        self.note = note
        self.log.error("%s", note)
        task = tasks.get(self.task)
        if fatal or task.oneshot:
            self.mode = "paused"
            return
        self._setbacks = getattr(self, "_setbacks", 0) + 1
        # Say when it is the SAME failure again. "setback 3/6" tells you the
        # budget is draining; "the same failure 3 times" tells you it is
        # deterministic and no amount of relogging will fix it.
        same = (note == getattr(self, "_last_setback", None))
        self._last_setback = note
        if same:
            self.log.error("this is the SAME failure %d times in a row - "
                           "relogging will not fix a deterministic error",
                           self._setbacks)
        if self._setbacks > self.MAX_SETBACKS:
            self.note = (f"{note} - and that is {self._setbacks} setbacks in a "
                         f"row, so stopping rather than thrashing. Look at the "
                         f"screen.")
            self.log.error("%s", self.note)
            self.mode = "paused"
            self._setbacks = 0
            return
        self.log.info("%s is a looping task, so recovering and carrying on "
                      "(setback %d/%d)", self.task, self._setbacks,
                      self.MAX_SETBACKS)
        # Back off a little, so a fault that recurs instantly cannot burn the
        # whole budget inside a second.
        time.sleep(min(3.0 * self._setbacks, 15.0))
        try:
            self.relog()
        except (OSError, CDPError) as e:
            raise Disconnected(str(e))
        except Exception as e:
            self.log.error("recovery relog failed: %s", e)

    def guard(self, fn, default=None):
        """Run a perception call, keeping the disconnect distinction intact.

        A dropped connection must NOT be swallowed as "the detector found
        nothing": the loop needs to reconnect, and a bot that reads a dead
        socket as an empty screen will happily walk into furniture forever.
        So OSError/CDPError still become `Disconnected`, and only genuine
        detector failures fall back to `default`.
        """
        try:
            return fn()
        except (OSError, CDPError) as e:
            raise Disconnected(str(e))
        except Exception:
            return default

    def step(self):
        """One cycle of the selected task. Blocks for as long as it takes."""
        self.cycle += 1
        task = tasks.get(self.task)

        # A task gets to handle the cycle BEFORE the ladder runs. That is where
        # "I am already in a mission" lives, because only the task knows
        # whether it can start from the middle of its own work.
        if task.preflight(self):
            return
        if not task.needs_lobby:
            return

        gray = cv2.cvtColor(self.cap.frame(gray=False), cv2.COLOR_BGR2GRAY)
        out, info = self.resumer.advance(gray)
        self.state = info.get("step", out)

        # A LEVEL-UP MOVES THE MISSION CEILING, and nothing else can tell the
        # farm that. `farm` remembers which page and row held the highest
        # unlocked mission so it does not re-walk seven pages every cycle, and
        # it verifies that memo before trusting it - but verification can only
        # catch a memo that became INVALID. After a level-up the remembered row
        # is still present and still unlocked, so the check passes and the bot
        # would keep farming the mission it was playing before it levelled,
        # never discovering the harder one that just opened.
        #
        # The ladder already recognises the Level Up panel (it has its own rung,
        # sweeping scales 1.20-1.80 because the check is drawn at 1.5), so the
        # cheap and reliable moment to forget the memo is right here.
        if self.state == "level_up":
            try:
                import farm as farm_mod
                farm_mod.forget_ceiling()
                self.log.info("levelled up - forgetting the remembered mission "
                              "ceiling so the list is read again")
            except Exception as e:
                self.log.warning("could not reset the mission ceiling: %s", e)
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
            # SAVE THE EVIDENCE THE FIRST TIME A SCREEN DEFEATS US.
            #
            # "Do nothing" is the right action on a screen we cannot name - it
            # is what stops a blind click - but on its own it teaches nobody
            # anything, and every such screen in this project was eventually
            # fixed by cutting ONE anchor from ONE frame. So capture that frame
            # while it is on screen, and say plainly that it needs teaching.
            if self.unknown == self.teach_at:
                self._save_for_teaching()
            if self.unknown >= self.max_unknown:
                streak = self.unknown
                self.unknown = 0
                self._relogs = 0
                self._setback(f"{streak} unrecognised frames in a row")
            return
        self.unknown = 0
        # The ladder got somewhere it recognises, so the relog budget is spent
        # on a DIFFERENT problem next time rather than staying exhausted.
        self._relogs = 0
        # NOTE the setback budget is deliberately NOT reset here. Reaching the
        # lobby is not progress - a relog always reaches the lobby, so a
        # deterministic failure reset the counter every lap and looped forever.
        # Observed live: "setback 1/6" over and over, never 2/6, while the bot
        # relogged endlessly on a mission-start crash. Only COMPLETED WORK
        # clears it; see the reset after `task.run` below.
        self.note = ""

        # Tasks may report that a cycle achieved NOTHING. Default to progress,
        # so a task that says nothing behaves as it always did.
        self._progress = True
        note = task.run(self)
        # A CYCLE THAT ACHIEVED SOMETHING IS THE ONLY THING THAT COUNTS.
        #
        # "The task returned without raising" is not enough, and a live loop
        # proved it. A mission runner crash is caught INSIDE `farm.farm`, which
        # logs it and breaks - so the task returned normally, having banked
        # nothing, and this cleared the setback budget every cycle. The log read
        # "setback 1/6" over and over while the same UnboundLocalError repeated
        # for ever, and the cap that exists to stop exactly that never counted
        # past one.
        if getattr(self, "_progress", True):
            self._setbacks = 0
        if note:
            # SET the note, do not LOG it. Every task already logs its own
            # summary through the module doing the work (`farm: 1 started, 1
            # banked`, `TP pass finished: ...`), so logging the returned note
            # printed each of them twice - which double-counts in a record
            # somebody will later use to work out what happened.
            self.note = note
        # A ONE-SHOT FINISHING IS AN ENDING; a lap finishing is not.
        #
        # The supervisor stays attached either way - the connection and the
        # panel belong to it, not to the task - so going quiet here costs
        # nothing and switching task remains immediate. What it must NOT do is
        # silently start the same one-shot again: a TP pass that re-ran itself
        # would keep re-walking a list it had just established was finished.
        if task.oneshot:
            self.mode = "ready"
            self.log.info("%s finished - ready (press Run, or pick another "
                          "task)", task.key)

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
            self._setback(f"mission runner: {type(e).__name__}: {e}")

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

    # SLEEP IS DETECTABLE, EVEN THOUGH IT CANNOT BE PREVENTED.
    #
    # On macOS `time.monotonic()` does not tick while the machine is asleep,
    # but the wall clock does - it is re-read from the RTC on wake. So the
    # difference between the two deltas across one loop iteration IS the time
    # spent suspended, and it distinguishes a sleep from a slow iteration,
    # which a wall-clock threshold alone cannot do: a mission legitimately
    # blocks for minutes.
    #
    # It degrades safely. If a platform's monotonic clock DOES include suspend
    # time, the difference stays near zero and this simply never fires - no
    # false relogs, which matters because a relog throws away an in-flight
    # mission.
    SLEPT_SECONDS = 60

    def _slept_for(self):
        """Seconds the machine was suspended since the last call, else 0."""
        w, m = time.time(), time.monotonic()
        prev = getattr(self, "_clocks", None)
        self._clocks = (w, m)
        if prev is None:
            return 0.0
        gap = (w - prev[0]) - (m - prev[1])
        return gap if gap >= self.SLEPT_SECONDS else 0.0

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
                # A SLEEP OUTLASTS THE GAME'S SESSION. The process resumes
                # exactly where it left off, so nothing raises - but the game
                # has been disconnected from its server for however long the
                # lid was shut, and every cached geometry hint now describes a
                # screen that is gone. Carrying on from stale beliefs is how a
                # bot spends ten minutes clicking a dead canvas.
                slept = self._slept_for()
                if slept:
                    self.log.info("the machine was asleep for %.0fs - the game "
                                  "session will not have survived that; "
                                  "relogging", slept)
                    try:
                        self.relog()
                        # Same reasoning as applying a window size: the drift
                        # cache and the alignment flag are keyed to the layout
                        # we had before, so keeping them aims every click at a
                        # screen that no longer exists.
                        self.cap._off_at = 0.0
                        self.focus_aligned = False
                    except Exception as e:
                        self.log.error("relog after wake failed: %s", e)

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
                        self._setback(f"task error: {type(e).__name__}: {e}")
                self.push()
            except Disconnected as e:
                # RESUME WHAT WE WERE DOING. This used to leave `mode` paused
                # even when the reconnect SUCCEEDED, so a single transient CDP
                # hiccup stopped a farm loop for good and the operator had to
                # notice and press Run. A restored connection is not a reason
                # to stop working.
                was = self.mode
                self.log.info("disconnected (%s)", e)
                self.mode = "paused"
                if not self.reconnect():
                    break
                if was == "running":
                    self.mode = "running"
                    self.log.info("reconnected - resuming %s", self.task)
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


def _proc_cmd(pid):
    """The command line of `pid`, or "" if it cannot be read."""
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _lock_holder(path, marker="app.py"):
    """The pid of a LIVE instance of this program, or None.

    Verifies identity, not just liveness - see the note at the call site. A lock
    naming a recycled pid, or one whose process is no longer this program, is
    treated as stale and removed, so a launch is never refused by a stranger.
    """
    try:
        with open(path) as f:
            raw = f.read().strip()
    except OSError:
        return None
    try:
        rec = json.loads(raw)
        pid, saved = int(rec.get("pid", 0)), rec.get("cmd", "")
    except Exception:
        # An older, bare-pid lock. Accept it, but hold it to the same test.
        try:
            pid, saved = int(raw or 0), ""
        except ValueError:
            pid, saved = 0, ""
    if not pid:
        return None
    try:
        os.kill(pid, 0)                       # raises unless it is alive
    except (ProcessLookupError, PermissionError, OSError):
        _drop_lock(path)
        return None
    cmd = _proc_cmd(pid)
    # IDENTITY FIRST. If the lock recorded the holder's command line, the live
    # process must still have exactly that - which is what distinguishes "our
    # instance is running" from "the OS handed that number to something else".
    # The marker is only the fallback for a legacy lock that recorded no
    # command, and it is deliberately not applied when identity already matched:
    # a legitimate launch may not have "app.py" in its command line at all.
    if saved:
        if cmd and cmd == saved:
            return pid
        _drop_lock(path)
        return None
    if cmd and marker not in cmd:
        _drop_lock(path)
        return None
    return pid


def _drop_lock(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_control(value):
    p = os.path.join(ROOT, "run/bot.control")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(value)


def _dead(pid):
    """Is `pid` really gone? Treats a ZOMBIE as gone, because it is.

    `os.kill(pid, 0)` succeeding does NOT mean anything is running - a pid
    lingers in the process table as a zombie until its parent reaps it, and
    `kill(0)` happily succeeds against one. Measured: a helper that lived three
    seconds was still reported alive twenty seconds later, because the poller
    was its parent and had not reaped it.

    This is the same trap this project already recorded for the pid lock: a
    bare pid proves something exists, never that it is alive and ours.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return True                       # no such process
    except Exception:
        return False
    # It exists. A zombie is finished, so ask the OS for its state. No `ps` on
    # Windows, where a finished process's handle simply goes away instead.
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
        return out.strip().upper().startswith("Z")
    except Exception:
        return False


def _wait_for_exit(pid, timeout=10.0, step=0.2):
    """Block until `pid` is gone. True if it went, False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _dead(pid):
            return True
        time.sleep(step)
    return _dead(pid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--config", default=os.path.join(ROOT, "Configs/mission.json"))
    ap.add_argument("--attach", action="store_true",
                    help="use a browser that is already running on --port "
                         "instead of launching an app window")
    ap.add_argument("--no-dock", action="store_true",
                    help="skip the panel (headless-ish; controls via run/bot.control)")
    ap.add_argument("--wait-for-pid", type=int, default=0,
                    help=argparse.SUPPRESS)   # set by Stop's own relaunch
    ap.add_argument("--no-keep-awake", action="store_true",
                    help="let the machine idle normally while the bot runs "
                         "(screen sleeps and locks, Teams presence goes Away)")
    a = ap.parse_args()

    # ONE RUNNER AT A TIME. Nothing stopped a second instance attaching to the
    # same tab, and instances stack silently: eight of them were found running
    # together, each pushing its own state into the shared panel every second and
    # each CLICKING THE GAME. From the outside that looks like the panel toggling
    # at random and the bot fighting itself - which is exactly what it was.
    #
    # A BARE PID IS NOT ENOUGH: PIDS GET REUSED. `os.kill(pid, 0)` only proves
    # SOMETHING is alive, not that it is us. Observed live - the lock held a pid
    # from a long-dead instance, the OS had recycled that number for an
    # unrelated process, and the launch was refused with "another bot window is
    # already running" while no bot was running at all. The operator's only
    # recourse was to delete the lock, which is exactly the habit that let eight
    # instances stack up in the first place.
    #
    # So the lock records WHO, and a claimant must match: same pid AND a command
    # line that still looks like this program.
    lock = os.path.join(ROOT, "run/app.lock")
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    other = _lock_holder(lock)
    if other and other != os.getpid():
        print(f"  another bot window is already running (pid {other}). "
              f"Close it first, or: kill {other}", flush=True)
        return 3
    with open(lock, "w") as f:
        json.dump({"pid": os.getpid(), "cmd": _proc_cmd(os.getpid())}, f)

    cfg = json.load(open(a.config))
    url = cfg.get("target", {}).get("game_url", "")
    profile = os.path.join(ROOT, "run/chrome-profile")

    # WAIT FOR THE PROCESS WE ARE REPLACING to actually be gone.
    #
    # Set only by Stop's own relaunch. Without it the incoming and outgoing
    # bots overlap for a moment and BOTH click the same game - the precise
    # duplicate the pid lock exists to prevent, created by the reset that
    # releases that lock on purpose. Polling the pid is exact, where a sleep on
    # either side would be a guess.
    if a.wait_for_pid:
        if _wait_for_exit(a.wait_for_pid, timeout=10):
            pass
        else:
            print(f"  pid {a.wait_for_pid} has not gone after 10s; continuing "
                  f"anyway (it releases the lock before spawning us and does "
                  f"not act afterwards, so this is belt and braces)",
                  flush=True)

    # FAIL FAST AND SAY WHAT TO DO. `--attach` with no browser on the port
    # used to poll `find_page_target` for 30-40 s and then raise "no page
    # target after 40s" - forty seconds of silence followed by a traceback,
    # which reads exactly like a hang. `cdp_ready` answers the same question
    # in 1.5 s.
    #
    # The advice matters as much as the speed: the site's session cookie is a
    # BROWSER-SESSION cookie, so a Chrome that has quit has also signed out,
    # and relaunching lands on the logged-out page. The bot must never
    # authenticate, so that last step is the operator's and saying so here
    # saves them discovering it via a halt several minutes later.
    if a.attach and not browser.cdp_ready(a.port):
        log_early = Log()
        log_early.error("--attach was given, but nothing is serving CDP on "
                        "port %d, so there is no browser to attach to.",
                        a.port)
        log_early.error("Launch one instead:  .venv/bin/python engine/app.py")
        log_early.error("Chrome having quit also means the game signed out "
                        "(its session cookie is a browser-session cookie), so "
                        "sign in once in the new window by hand - the bot "
                        "never enters credentials.")
        # Only ever release a lock we hold - the same rule `_hard_exit` and
        # the caller's `finally` follow. Unlinking unconditionally is how a
        # guard ends up deleting a legitimate holder's lock.
        if _lock_holder(lock) == os.getpid():
            _drop_lock(lock)
        return 2

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
    _cap_cv_threads(log)

    # A run is long and hands-off, so the machine would idle out from under it:
    # display asleep, screen locked, Teams presence Away. See engine/presence.py.
    keeper = presence.KeepAwake(log)
    if not a.no_keep_awake:
        keeper.start()

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
        try:
            keeper.stop()
        except Exception:
            pass
        c.close()
        try:
            if _lock_holder(lock) == os.getpid():
                os.unlink(lock)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
