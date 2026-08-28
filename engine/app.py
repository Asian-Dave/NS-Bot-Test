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
        # Focus mode is armed by default and applied as soon as the game is
        # detected. It is not decoration: hiding the page chrome lets the game
        # reflow to the top so scrollY is 0 and STAYS 0, which is what makes the
        # bot's coordinates mean the same thing from one minute to the next.
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

    def push(self):
        try:
            self.dock.render({
                "mode": self.mode, "state": self.state, "task": self.task,
                "cycle": self.cycle, "uptime": self._uptime(),
                "note": self.note, "tasks": TASKS, "log": self.log.lines[-10:],
                "focus": self.focus_on,
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
            # STOPS THE BOT, NOT THE PANEL. Stop used to end the process, and the
            # dock vanished with it - the injection lives in the CDP session that
            # created it, so when the process goes, so does the panel. Pressing
            # Stop and losing your control panel is not what Stop means. Use Quit
            # to actually exit.
            self.mode = "stopped"
            _write_control("stop")
            self.log.info("operator: STOP (panel stays; press Run to resume)")
        elif c == "focus":
            self.focus_wanted = not self.focus_wanted
            self.dock.focus(self.focus_wanted)
            self.focus_on = self.focus_wanted
            self.log.info("operator: focus mode %s",
                          "ON" if self.focus_wanted else "off")
        elif c == "quit":
            self.quit = True
            _write_control("stop")
            self.log.info("operator: QUIT - closing the panel and exiting")
        elif c == "task":
            if any(t["key"] == cmd.get("arg") for t in TASKS):
                self.task = cmd["arg"]
                self.log.info("operator: task -> %s", self.task)
        elif c == "relog":
            # Reload only. NEVER authenticate: CLAUDE.md is explicit that a
            # logged-out session may mean a password change or a ban, and that
            # it must surface to the human rather than be auto-recovered.
            self.log.info("operator: RELOG (reload only - never authenticates)")
            self.cdp.call("Page.reload")
            time.sleep(4.0)
            browser.pin_viewport(self.cdp, *VIEWPORT)

    def pump(self, poll=0.25):
        for cmd in self.dock.commands(poll=poll):
            self._apply(cmd)

    # -- work --------------------------------------------------------------
    def step(self):
        """One cycle of the selected task. Blocks for as long as it takes."""
        self.cycle += 1
        if self.task == "idle":
            self.state = "idle"
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
        if self.state in ("unknown",) or out == "unknown":
            # THE LADDER'S "unknown" IS NOT THE OPERATOR'S "unknown". The resume
            # ladder only knows login -> lobby and result panels; a battle, a
            # cutscene or a minigame is not its job, so it correctly reports
            # "unknown" and the panel then showed `state: unknown` for the whole
            # of a perfectly healthy mission. That is alarming and wrong.
            # Ask the classifier that DOES know those screens before giving up
            # on naming what we are looking at.
            try:
                import minigame as mg
                kind, _ = mg.classify(self.cap.frame(gray=False))
                if kind != mg.UNKNOWN:
                    self.state = kind
            except Exception:
                pass

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
            if self.unknown >= self.max_unknown:
                self.note = (f"{self.unknown} unrecognised frames in a row - "
                             f"pausing rather than spinning. Look at the screen.")
                self.log.error("%s", self.note)
                self.mode = "paused"
                self.unknown = 0
            return
        self.unknown = 0
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
                                            self.cfg, self.controls, repeat=1)
            self.note = f"farm: {started} started, {banked} banked"
            self.log.info(self.note)

    def reconnect(self):
        """Rebuild the connection after the socket dies.

        LOGGING OUT KILLS THE SOCKET. The page navigates, Chrome tears the target
        down, and every later call raises BrokenPipeError. Without this the
        process stayed alive doing nothing but logging "panel update failed"
        forever, and the only way to get the panel back was to restart by hand —
        which is exactly the thing the dock is supposed to save you from.
        """
        self.log.info("connection lost - reconnecting")
        for attempt in range(30):
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
            self.resumer = resume.Resumer(cap, actor, self.tpls, self.log,
                                          controls=self.controls)
            self.log.info("reconnected; the panel is back")
            return True
        self.log.error("could not reconnect after 30 attempts")
        return False

    def ensure_focus(self):
        """Apply focus mode once the game is on the page.

        Waits for the game rather than doing it on load: before sign-in there is
        no game to focus on, and hiding the login page would leave the operator
        staring at nothing.
        """
        if self.focus_on and not self.focus_aligned:
            # One re-align after the layout has settled. Converges and stops.
            try:
                if self.dock.align() in ("realigned", "aligned"):
                    self.focus_aligned = True
            except Exception:
                pass
        if not self.focus_wanted or self.focus_on:
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
        while not self.quit:
            try:
                self.ensure_dock()
                self.ensure_focus()
                self.pump()
                if self.mode == "running":
                    try:
                        self.step()
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
        self.push()
        self.log.info("quit")


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
