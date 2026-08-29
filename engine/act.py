"""Act layer — clicks, pacing, and the stop switch.

Why not pydirectinput, as originally specified: it executes
`ctypes.windll.user32.SendInput` at import time and is classified Windows-only,
so it cannot even be imported here. The concern behind choosing it was real
though - that synthetic events get ignored by game canvases. CDP answers that
properly: `Input.dispatchMouseEvent` is injected below the JS layer, and events
were verified arriving at the live Ruffle canvas with `isTrusted: true`.

Pacing: every click gets a randomised delay drawn from a configured range plus a
small coordinate jitter, so timing and positioning are not robotically uniform.
"""
import random
import time


class Actor:
    def __init__(self, cdp, capture, log, dry_run=False,
                 click_delay=(0.18, 0.55), jitter_px=3, post_click=(0.4, 1.1),
                 no_click_zones=()):
        self.cdp, self.capture, self.log = cdp, capture, log
        self.dry_run = dry_run
        self.click_delay = click_delay
        self.jitter_px = jitter_px
        self.post_click = post_click
        # Rectangles, in CAPTURED px, that the bot must never click into.
        # The control dock is one: it is injected into the game page, so its
        # buttons are as clickable as anything else on screen, and a stray bot
        # click there would press Stop or switch the task by accident. Every
        # measured target is inside the game rect, so this should never fire -
        # but "should never fire" is exactly what was believed about the fixed
        # card grid right before it clicked into the weapon Shop.
        self.no_click_zones = list(no_click_zones)

    def _sleep(self, rng):
        time.sleep(random.uniform(*rng))

    def blocked_by(self, px, py):
        """The no-click zone containing this point, if any."""
        for (zx, zy, zw, zh) in self.no_click_zones:
            if zx <= px < zx + zw and zy <= py < zy + zh:
                return (zx, zy, zw, zh)
        return None

    def click_pixel(self, px, py, why=""):
        """Click a point given in CAPTURED PIXELS (what the matcher returns).

        Conversion to CSS coordinates happens here and nowhere else, so callers
        never need to know the device pixel ratio.
        """
        zone = self.blocked_by(px, py)
        if zone is not None:
            self.log.warning(
                "REFUSING click (%.0f,%.0f) %s - it lands in a no-click zone %s "
                "(the control dock). A bot click there would press the "
                "operator's own buttons.", px, py, why, zone)
            return None
        cx, cy = self.capture.to_click_coords(px, py)
        self._sleep(self.click_delay)
        if self.dry_run:
            self.log.info("DRY-RUN would click (%.0f,%.0f) css=(%.0f,%.0f) %s",
                          px, py, cx, cy, why)
            return None
        ax, ay = self.cdp.click(cx, cy, jitter=self.jitter_px)
        self.log.info("CLICK px=(%.0f,%.0f) css=(%d,%d) %s", px, py, ax, ay, why)
        self._sleep(self.post_click)
        return ax, ay

    def click_match(self, match, why=""):
        if not match.found:
            self.log.warning("refusing to click %s: not found", match.name)
            return None
        return self.click_pixel(*match.center, why=why or match.name)


class Controls:
    """Pause / stop switch.

    The brief asked for a global hotkey. That does not translate into a
    container - there is no host keyboard focus to hook, and a hotkey registered
    inside the container would only fire while the Xvfb window had focus, which
    defeats the point. A control file is the equivalent that actually works here:

        docker exec ns sh -c 'echo pause > /profile/bot.control'
        docker exec ns sh -c 'echo run   > /profile/bot.control'
        docker exec ns sh -c 'echo stop  > /profile/bot.control'

    It is inspectable, survives restarts, and works from the host shell without
    any host dependency. SIGINT/SIGTERM also stop cleanly, so `docker stop` and
    Ctrl-C behave sensibly.
    """

    def __init__(self, path="/profile/bot.control", log=None, on_wait=None):
        self.path, self.log = path, log
        # Called on every poll while paused. This exists because a paused task
        # would otherwise DEADLOCK an in-page control panel: the dock's Pause
        # button writes `pause` here, a long task blocks in `wait_if_paused`,
        # and the only code that could read the operator's next button press is
        # the loop that is now blocked inside the task. Measured live - Pause
        # then Run left the runner waiting forever.
        #
        # Giving the waiter a callback keeps pause working MID-TASK, which is
        # the behaviour worth having: a mission takes minutes and "you may
        # un-pause once it finishes" is not a pause.
        self.on_wait = on_wait
        self._stopped = False
        import signal
        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, self._on_signal)

    def _on_signal(self, *_):
        self._stopped = True
        if self.log:
            self.log.warning("signal received - stopping after current cycle")

    def state(self):
        if self._stopped:
            return "stop"
        try:
            with open(self.path) as f:
                v = f.read().strip().lower()
            return v if v in ("run", "pause", "stop") else "run"
        except FileNotFoundError:
            return "run"

    def wait_if_paused(self, poll=0.5):
        """Block while paused. Returns False if we should stop entirely.

        `on_wait` is pumped every poll so whoever owns the operator's input can
        still see it while we are parked here - see the note in __init__.
        """
        said = False
        while True:
            s = self.state()
            if s == "stop":
                return False
            if s != "pause":
                # PUMP EVEN WHEN NOT PAUSED. This is called from the gate's poll
                # loop, which is where the bot spends a whole mission, so it is
                # the only regular opportunity to service operator input and to
                # tell the panel we are still alive. Pumping only while PAUSED
                # meant the panel went untouched for minutes during a mission and
                # its own staleness watchdog declared "no bot attached - the
                # panel is frozen" while the bot was working perfectly.
                if self.on_wait is not None:
                    try:
                        self.on_wait()
                    except Exception:
                        pass
                return True
            if self.log and not said:
                self.log.info("paused - waiting (%s)", self.path)
                said = True
            if self.on_wait is not None:
                try:
                    self.on_wait()
                except Exception:
                    pass
            time.sleep(poll)


class fast_pacing:
    """Temporarily tighten an Actor's click pacing, then restore it.

    `Actor`'s defaults sleep 0.18-0.55 s before and 0.4-1.1 s after every click -
    up to 1.65 s each. That randomised pacing is anti-robotic cosmetics for
    wandering the village, and it is the wrong trade anywhere the game is waiting
    on us: in a battle it is most of the delay between "it is our turn" and the
    action actually going out, which reads as the bot being slow to think.

    Used by the minigame solvers and the battle runner. `cards.py` keeps its own
    copy of this idea deliberately - it is the module the operator asked not to
    disturb.
    """

    def __init__(self, actor, click=(0.02, 0.06), post=(0.02, 0.06)):
        self.a, self.click, self.post = actor, click, post

    def __enter__(self):
        self._save = (self.a.click_delay, self.a.post_click)
        self.a.click_delay, self.a.post_click = self.click, self.post
        return self.a

    def __exit__(self, *exc):
        self.a.click_delay, self.a.post_click = self._save
        return False
