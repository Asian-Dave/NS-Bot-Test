#!/usr/bin/env python3
"""Keep the machine out of the idle state for as long as the bot window is up.

WHY THIS BELONGS TO THE BOT
---------------------------
A farming run is precisely the case the OS idle timer exists to catch: the bot
clicks the game for an hour while the operator touches nothing. macOS scores
that as an idle machine - the display sleeps, the screen locks, and Teams flips
the presence badge to Away. Watching a bot work should not read as being away
from the desk.

Teams does not track Teams activity. It reads the system-wide HID idle timer
(IOHIDSystem's `HIDIdleTime`), so any REAL input resets it. The bot's own
clicks cannot: CDP `Input.dispatchMouseEvent` synthesises events inside
Chrome's own event loop, which never touches the OS HID layer, so an hour of
botting leaves the counter climbing exactly as if the machine were abandoned.

WHAT RESETS THE COUNTER (measured, this machine)
------------------------------------------------
    caffeinate -u -t 1        idle 39.3s -> 40.4s    NO  - display assertion only
    osascript `key code 63`   idle 62.8s ->  0.2s    YES - reproducible

`key code 63` is the `fn` key: it resets the timer, types no character and
moves no cursor, so it cannot corrupt whatever window happens to hold focus -
which matters here, because the focused window is usually the game.

We only inject once the machine has been genuinely idle past THRESHOLD, so an
operator who is actually at the keyboard never has synthetic events landing on
top of their typing. Worst case the counter reaches THRESHOLD + INTERVAL
(150 s by default), comfortably under the 300 s at which Teams says Away.

WHY A THREAD AND NOT THE STANDALONE `keepgreen` SCRIPT
-----------------------------------------------------
Shelling out to an external daemon leaks one. `kill -9` on the bot - the
operator's habit when a run wedges - skips every `finally` in `app.py`, and a
detached daemon would then hold the machine awake indefinitely with nothing
left to turn it off. A `daemon=True` thread cannot outlive the process it is
in, so the guarantee is structural rather than a promise about cleanup paths.

It is also independent of any `keepgreen` the operator started by hand: both
merely reset the same counter, so neither can cancel the other, and the bot
exiting never revokes a keep-awake the operator set up for their own reasons.

Presence is a convenience. It must never take the bot down with it, so every
failure here is logged once and swallowed.
"""
import platform
import subprocess
import threading

THRESHOLD = 120   # only inject after this many seconds of real idle
INTERVAL = 30     # how often to look

_POKE = ["osascript", "-e", 'tell application "System Events" to key code 63']


def idle_seconds():
    """System-wide seconds since the last real keyboard or mouse input.

    Reads the same counter Teams reads. Returns 0.0 if it cannot be read, so a
    parse failure fails SAFE - no poke - rather than injecting blindly.
    """
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0.0
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            try:
                return int(line.rsplit("=", 1)[1].strip()) / 1e9
            except ValueError:
                return 0.0
    return 0.0


class KeepAwake:
    """Holds the machine awake until `stop()`, or until the process dies."""

    def __init__(self, log, threshold=THRESHOLD, interval=INTERVAL):
        self.log, self.threshold, self.interval = log, threshold, interval
        self._stop = threading.Event()
        self._thread = None
        self._warned = False
        self._pokes = 0

    def start(self):
        # No-op off macOS: `ioreg` and `osascript` are the whole mechanism.
        if platform.system() != "Darwin":
            return self
        self._thread = threading.Thread(target=self._loop, name="keep-awake",
                                        daemon=True)
        self._thread.start()
        self.log.info("keep-awake on (poke after %ds idle)", self.threshold)
        return self

    def stop(self):
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=2)
        self._thread = None
        self.log.info("keep-awake off (%d pokes)", self._pokes)

    def _loop(self):
        # `wait` IS the sleep: it returns early when stop() fires, so shutdown
        # never blocks for a whole interval.
        while not self._stop.wait(self.interval):
            try:
                if idle_seconds() >= self.threshold:
                    self._poke()
            except Exception as e:                      # never kill the bot
                self._warn("keep-awake: %s" % e)

    def _poke(self):
        r = subprocess.run(_POKE, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            self._pokes += 1
            return
        # Overwhelmingly this is the Accessibility grant: osascript reports
        # "not allowed to send keystrokes (1002)" until the app that launched
        # the bot is ticked in System Settings > Privacy & Security >
        # Accessibility. Say it once, then stay quiet - it will not fix itself
        # mid-run and a per-interval warning would bury the mission log.
        self._warn("keep-awake disabled: %s" % (r.stderr or "").strip()[:90])
        self._stop.set()

    def _warn(self, msg):
        if not self._warned:
            self._warned = True
            self.log.warning(msg)
