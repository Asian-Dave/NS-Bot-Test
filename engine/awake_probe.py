#!/usr/bin/env python3
"""Measure whether the game keeps RENDERING while the screen is locked.

WHY THIS EXISTS AS A SEPARATE TOOL
----------------------------------
"Does the bot survive a lock screen?" cannot be answered by reasoning about
Chrome's flags, and it cannot be answered from inside a session that is
watching the screen - the act of looking requires the screen. So it is
measured the only honest way: sample continuously, let the operator lock the
machine, and read the record afterwards.

WHAT IS ACTUALLY BEING MEASURED
-------------------------------
`requestAnimationFrame` rate, in the page. Ruffle's render loop is rAF-driven
(see the flag comment in `browser.py`: an occluded window measured ZERO
callbacks per 1500 ms), so the rAF rate IS whether the game is running. A
frozen rAF means the SWF is not advancing, which means captures show a stale
frame and every template match is against the past.

Deliberately NOT screenshots. CLAUDE.md records that a second CDP client
taking CLIPPED screenshots re-applies device metrics and RESIZES the page
under the bot - stray clicks landed on the dock and pressed Relog. Reading a
counter out of the page costs one `Runtime.evaluate` and cannot move anything.

HOW TO USE IT
-------------
    .venv/bin/python engine/awake_probe.py --seconds 150

Then lock the screen (ctrl-cmd-Q), wait about a minute, and unlock. The log
lands in `run/awake_probe.log` with wall-clock timestamps, so the locked
window is identifiable after the fact:

    fps holds near 24 across the gap  -> the lock screen is survivable
    fps drops to 0.0 and recovers     -> the renderer was suspended

`--fix` additionally asserts focus emulation and an active lifecycle state, so
the two runs can be compared rather than argued about.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Counts rAF callbacks in the page. Installed once, read repeatedly, and it
# keeps a WALL-CLOCK stamp of its own last tick: if the renderer is suspended
# the counter simply stops moving, and the stamp says when it stopped, which a
# sample-side clock cannot tell us.
INSTALL = """
(() => {
  if (window.__nsprobe) return "already";
  window.__nsprobe = {n: 0, last: Date.now()};
  const tick = () => {
    window.__nsprobe.n++;
    window.__nsprobe.last = Date.now();
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return "installed";
})()
"""

READ = """
JSON.stringify({
  n: window.__nsprobe ? window.__nsprobe.n : -1,
  last: window.__nsprobe ? window.__nsprobe.last : 0,
  vis: document.visibilityState,
  focus: document.hasFocus(),
  now: Date.now()
})
"""


def page_socket(port=9222):
    tabs = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json/list", timeout=5))
    pages = [t for t in tabs if t.get("type") == "page"
             and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise SystemExit("no page target - is the bot window open?")
    return pages[0]["webSocketDebuggerUrl"]


def evaluate(c, expr):
    r = c.call("Runtime.evaluate", expression=expr, returnByValue=True)
    return r["result"].get("value")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=150)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--fix", action="store_true",
                    help="also assert focus emulation + active lifecycle, to "
                         "compare against a plain run")
    a = ap.parse_args()

    c = cdp.CDP(page_socket(a.port))
    print("installed:", evaluate(c, INSTALL))

    if a.fix:
        # Neither call needs a browser restart, which is the whole point: a
        # relaunch would cost the session cookie and therefore a manual
        # sign-in (CLAUDE.md: the session does NOT survive quitting Chrome).
        for method, params in (
                ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                ("Page.setWebLifecycleState", {"state": "active"})):
            try:
                c.call(method, **params)
                print("asserted:", method)
            except Exception as e:
                print("could not assert", method, "-", e)

    path = os.path.join(ROOT, "run/awake_probe.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    prev_n, prev_t = None, None
    stop = time.time() + a.seconds

    with open(path, "w") as fh:
        head = (f"# rAF probe, {a.interval}s samples, fix={a.fix}\n"
                f"# lock the screen now; unlock before the end\n"
                f"# {'time':<9} {'fps':>6} {'frames':>7} {'stall_s':>8} "
                f"{'vis':<8} focus\n")
        fh.write(head)
        fh.flush()
        print(head, end="")

        while time.time() < stop:
            time.sleep(a.interval)
            try:
                s = json.loads(evaluate(c, READ))
            except Exception as e:
                # A suspended renderer can make the evaluate itself hang or
                # fail. That is DATA, not an error - record and carry on.
                line = f"  {time.strftime('%H:%M:%S')}  evaluate failed: {e}\n"
                fh.write(line); fh.flush(); print(line, end="")
                continue

            now = time.time()
            fps = frames = 0.0
            if prev_n is not None and now > prev_t:
                frames = s["n"] - prev_n
                fps = frames / (now - prev_t)
            prev_n, prev_t = s["n"], now

            # How long ago the page itself last drew, by the PAGE's clock. A
            # renderer that was suspended and resumed shows a large stall here
            # even though the sample interval looked normal.
            stall = max(0.0, (s["now"] - s["last"]) / 1000.0)
            line = (f"  {time.strftime('%H:%M:%S')} {fps:>6.1f} {frames:>7.0f} "
                    f"{stall:>8.1f} {s['vis']:<8} {s['focus']}\n")
            fh.write(line); fh.flush(); print(line, end="")

    c.close()
    print(f"\nwritten to {path}")
    print("Read the rows between your lock and unlock: fps ~24 means the game "
          "kept running; 0.0 means the renderer was suspended.")


if __name__ == "__main__":
    main()
