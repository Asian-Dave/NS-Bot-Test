"""State gates — wait until one of several known things is true.

THE IDEA, AND WHERE IT CAME FROM
--------------------------------
This is the one piece of the reference bot (`ref/tp/cmmhero`) worth copying
wholesale. Its entire control flow rests on a single primitive
(`FormMain.cs:14602`):

    Task<int> PixelLoop(List<(x, y, colour)> conditions, int timeout)

It polls, and returns the INDEX of whichever condition became true first. Not a
bool — the index. Every branch in that bot is then "wait for one of these five
things, then switch on which one happened". There is not a single fixed sleep in
its battle path.

That matters for us because CLAUDE.md already records the failure it prevents:
clicks issued during the enemy's turn are silently discarded, and roughly a
third of a mission's actions were lost to fixed-schedule clicking before we
switched to detection. "Detect, then act" was the lesson; this is the lesson as
a reusable function.

WHAT WE CHANGE
--------------
Their conditions are exact single-pixel colour probes, which works because they
force one window size. Ours must survive a varying canvas (see geometry.py), so a
Condition here is a PREDICATE over a frame and can be any of:

    * a template match       — our primary mechanism, scale-swept
    * a pixel probe          — cheap, exact, for flat UI chrome
    * an arbitrary callable  — anything you can measure

Mixing them in one gate is the point. "Wait for the Victory panel OR the command
bar OR a loading screen" needs two template checks and a colour read, and the
caller should not have to know which is which.

PIXEL PROBES ON OUR CLIENT
--------------------------
Exact-match pixel probing is viable here for the same reason it is viable for
them: Ruffle rasterises deterministically, which CLAUDE.md already notes as the
reason digit templates would beat OCR. But two caveats we must respect and they
did not have to:

  * Coordinates are only valid for one canvas geometry, so a probe should be
    expressed relative to a BattleGeometry anchor, or confined to page chrome
    that does not move.
  * Anti-aliased edges and animated art will not reproduce exactly. Probe flat
    interior colour, never an edge. `tolerance` exists for the marginal cases;
    default 0 keeps you honest about which you are relying on.
"""
import time

import cv2

from perceive import find


class Condition:
    """One named thing that might be true of a frame.

    `check(frame_bgr, frame_gray)` returns falsy, or a truthy payload. The
    payload is passed back to the caller, so a template condition can hand back
    the match (and therefore a click point) rather than just "yes".
    """

    def __init__(self, name, check, note=""):
        self.name = name
        self.check = check
        self.note = note

    def __repr__(self):
        return f"<Condition {self.name}>"


def template(name, tpl, threshold=None):
    """Condition: `tpl` is on screen. Payload is the Match."""
    def _check(_bgr, gray):
        m, conf = find(gray, tpl)
        if threshold is not None:
            return m if conf >= threshold else None
        return m if m.found else None
    return Condition(name, _check, note=f"template {tpl.name}")


def pixel(name, x, y, bgr, tolerance=0):
    """Condition: pixel (x, y) equals `bgr`, within `tolerance` per channel.

    Coordinates are CAPTURED PIXELS, the same space the matcher and
    `Actor.click_pixel` use.
    """
    b, g, r = bgr

    def _check(frame_bgr, _gray):
        h, w = frame_bgr.shape[:2]
        if not (0 <= x < w and 0 <= y < h):
            return None
        px = frame_bgr[y, x]
        if tolerance == 0:
            ok = int(px[0]) == b and int(px[1]) == g and int(px[2]) == r
        else:
            ok = (abs(int(px[0]) - b) <= tolerance
                  and abs(int(px[1]) - g) <= tolerance
                  and abs(int(px[2]) - r) <= tolerance)
        return (int(px[0]), int(px[1]), int(px[2])) if ok else None
    return Condition(name, _check, note=f"pixel({x},{y})=={bgr} tol={tolerance}")


def predicate(name, fn, note=""):
    """Condition from any callable(frame_bgr, frame_gray) -> falsy | payload."""
    return Condition(name, fn, note=note)


class Fired:
    """Which condition became true, and what it saw."""

    def __init__(self, index, name, payload, elapsed, polls):
        self.index, self.name = index, name
        self.payload, self.elapsed, self.polls = payload, elapsed, polls

    def __bool__(self):
        return True

    def __repr__(self):
        return (f"<Fired {self.name} idx={self.index} "
                f"after={self.elapsed:.2f}s polls={self.polls}>")


class TimedOut:
    """No condition became true inside the timeout.

    Deliberately a distinct object rather than None, so the caller can tell
    "nothing happened" apart from "I was told to stop". Falsy, so
    `if gate.wait_for_any(...)` reads naturally.
    """

    def __init__(self, elapsed, polls, names):
        self.elapsed, self.polls, self.names = elapsed, polls, names

    def __bool__(self):
        return False

    def __repr__(self):
        return f"<TimedOut after={self.elapsed:.2f}s polls={self.polls}>"


class Stopped:
    """The control file or a signal asked us to stop mid-wait."""

    def __init__(self, elapsed, polls):
        self.elapsed, self.polls = elapsed, polls

    def __bool__(self):
        return False

    def __repr__(self):
        return f"<Stopped after={self.elapsed:.2f}s>"


class Gate:
    """Polling state gate over a live capture.

    Holds the capture and (optionally) the pause/stop controls, so a long wait
    stays interruptible — a 90s loading wait that ignores the stop switch is a
    bot you cannot turn off.
    """

    def __init__(self, capture, log, controls=None, poll_interval=0.25):
        self.capture, self.log, self.controls = capture, log, controls
        self.poll_interval = poll_interval

    def _frames(self, clip=None):
        bgr = self.capture.frame(gray=False, clip=clip)
        return bgr, cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    def check_now(self, conditions, clip=None):
        """Evaluate once against a single fresh capture. No waiting."""
        bgr, gray = self._frames(clip)
        for i, c in enumerate(conditions):
            payload = c.check(bgr, gray)
            if payload:
                return Fired(i, c.name, payload, 0.0, 1)
        return TimedOut(0.0, 1, [c.name for c in conditions])

    def wait_for_any(self, conditions, timeout, clip=None, why=""):
        """Poll until one condition fires, or `timeout` seconds elapse.

        Returns Fired / TimedOut / Stopped — all of which are falsy except Fired,
        so the common case reads as a plain truth test while the caller can still
        distinguish a timeout from a stop when it matters.

        Conditions are evaluated IN ORDER on each poll, so put the
        highest-priority state first. That ordering is not cosmetic: CLAUDE.md
        records that the Victory panel draws OVER the command bar, so a gate
        listing the command bar before the result panel would report "my turn"
        on a frame where the fight is already over. Priority order, never
        presence alone.
        """
        t0 = time.time()
        polls = 0
        names = [c.name for c in conditions]
        while True:
            if self.controls is not None and not self.controls.wait_if_paused():
                return Stopped(time.time() - t0, polls)
            polls += 1
            try:
                bgr, gray = self._frames(clip)
            except Exception as e:
                self.log.error("gate capture failed: %s", e)
                return TimedOut(time.time() - t0, polls, names)
            for i, c in enumerate(conditions):
                payload = c.check(bgr, gray)
                if payload:
                    el = time.time() - t0
                    self.log.info("gate[%s] -> %s after %.2fs (%d polls)",
                                  why or "/".join(names[:3]), c.name, el, polls)
                    return Fired(i, c.name, payload, el, polls)
            el = time.time() - t0
            if el >= timeout:
                self.log.warning("gate[%s] TIMEOUT after %.1fs (%d polls); "
                                 "waited on: %s", why or "?", el, polls,
                                 ", ".join(names))
                return TimedOut(el, polls, names)
            time.sleep(self.poll_interval)

    def wait_until_gone(self, condition, timeout, clip=None):
        """Poll until `condition` stops being true. The inverse gate.

        Needed for dismiss flows: after clicking an X you want to confirm the
        popup actually went away, not just that you clicked something. The login
        sequence queues four popups, and without this the drain loop cannot tell
        a successful dismiss from a click that missed.
        """
        t0 = time.time()
        polls = 0
        while True:
            if self.controls is not None and not self.controls.wait_if_paused():
                return Stopped(time.time() - t0, polls)
            polls += 1
            bgr, gray = self._frames(clip)
            if not condition.check(bgr, gray):
                el = time.time() - t0
                self.log.info("gate: %s cleared after %.2fs", condition.name, el)
                return Fired(0, f"{condition.name}:gone", None, el, polls)
            el = time.time() - t0
            if el >= timeout:
                self.log.warning("gate: %s still present after %.1fs",
                                 condition.name, el)
                return TimedOut(el, polls, [condition.name])
            time.sleep(self.poll_interval)
