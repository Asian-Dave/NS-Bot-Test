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
                 click_delay=(0.18, 0.55), jitter_px=3, post_click=(0.4, 1.1)):
        self.cdp, self.capture, self.log = cdp, capture, log
        self.dry_run = dry_run
        self.click_delay = click_delay
        self.jitter_px = jitter_px
        self.post_click = post_click

    def _sleep(self, rng):
        time.sleep(random.uniform(*rng))

    def click_pixel(self, px, py, why=""):
        """Click a point given in CAPTURED PIXELS (what the matcher returns).

        Conversion to CSS coordinates happens here and nowhere else, so callers
        never need to know the device pixel ratio.
        """
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

    def __init__(self, path="/profile/bot.control", log=None):
        self.path, self.log = path, log
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

    def wait_if_paused(self, poll=2.0):
        """Block while paused. Returns False if we should stop entirely."""
        while True:
            s = self.state()
            if s == "stop":
                return False
            if s != "pause":
                return True
            if self.log:
                self.log.info("paused - waiting (%s)", self.path)
            time.sleep(poll)
