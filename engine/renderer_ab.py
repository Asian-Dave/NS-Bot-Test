#!/usr/bin/env python3
"""Compare Ruffle's render backends against THIS bot's perception layer.

WHY THIS EXISTS
---------------
Ruffle picks a renderer at startup from the ladder
`webgpu` -> `wgpu-webgl` -> `webgl` -> `canvas`, and never announces which. On
this install the SITE asks for `wgpu-webgl` and gets a live `webgl2` context.

That would be a performance footnote for a human player. Here it is a
CALIBRATION question, because every template threshold and every colour range
in this project was measured against one backend. A different one draws the
same SWF with different filtering and antialiasing, so the pixels move
underneath a perception layer that has no way to notice - and
`docs/BENCHMARK.md` already records that a WebGL blocklist makes Ruffle drop
to canvas2d SILENTLY. A driver update is enough.

So the question this answers is not "which renderer is prettiest" but: **does
one calibration serve every backend, or do thresholds have to be per-renderer?**

WHAT IT MEASURES
----------------
For each backend, on the SAME game screen:

  * which context actually went live (asking for a backend is not getting it)
  * the rendered frame, saved for comparison
  * requestAnimationFrame rate - the honest measure of "render intensity",
    since Ruffle's whole render loop is rAF-driven
  * every template's score, so a threshold that moves is visible

HOW IT SWITCHES
---------------
`preferredRenderer` is read when the player is constructed, and the SITE sets
its own config first. So the override is installed with
`Page.addScriptToEvaluateOnNewDocument` - which runs BEFORE page scripts - and
then re-asserted on a short interval, because the site's own assignment lands
after ours. The probe then reports what actually went live, so a failed
override is visible rather than assumed away.

Every switch needs a RELOAD. A reload keeps the session (only quitting the
browser signs the game out), so this is safe to run on a live session - but it
does return the game to character select, and the operator has to be signed in
for the SWF to load at all.

    .venv/bin/python engine/renderer_ab.py                 # all backends
    .venv/bin/python engine/renderer_ab.py --only webgl    # just one
    .venv/bin/python engine/renderer_ab.py --score-only    # re-score saved frames
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2  # noqa: E402

import browser  # noqa: E402
import cdp  # noqa: E402
import perceive  # noqa: E402
from perceive import find  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "ref/auto/renderer")

# Ruffle's own ladder, in the order it would fall through them.
BACKENDS = ["webgpu", "wgpu-webgl", "webgl", "canvas"]


class _Log:
    def info(self, m, *a):
        print("  " + ((m % a) if a else m), flush=True)
    warning = error = info


def force_renderer(c, want):
    """Store the game's own `renderMode` so the site picks `want` on reload.

    NOT a Ruffle config override. Three attempts at that failed - see the long
    note in browser.py - because the site reads `localStorage.renderMode` on
    every load and passes its own config to `player.load()`. Writing the key
    the site already reads is the whole mechanism, and it is verified by
    reading it back.
    """
    return browser.write_game_setting(c, "renderMode", want)


def wait_for_ruffle(c, timeout=90):
    """Block until Ruffle has a canvas with a context. Returns the info dict."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = browser.renderer_info(c)
        if last.get("live"):
            return last
        time.sleep(1.0)
    return last


def measure_fps(c, seconds=3.0):
    """rAF callbacks per second, in the game's own frame.

    This is the render-intensity number that matters: Ruffle's render loop IS
    requestAnimationFrame, so a backend that cannot keep up shows here.
    """
    setup = """
    (() => {
      const f = document.querySelector('iframe[src*="emulator"], iframe[src*="play"]');
      const w = f ? f.contentWindow : window;
      if (!w) return "no frame";
      w.__ab = {n: 0};
      const tick = () => { w.__ab.n++; w.requestAnimationFrame(tick); };
      w.requestAnimationFrame(tick);
      return "ok";
    })()
    """
    read = """
    (() => {
      const f = document.querySelector('iframe[src*="emulator"], iframe[src*="play"]');
      const w = f ? f.contentWindow : window;
      return String((w && w.__ab) ? w.__ab.n : -1);
    })()
    """
    c.evaluate(setup)
    time.sleep(0.4)
    a = int(c.evaluate(read) or -1)
    t0 = time.time()
    time.sleep(seconds)
    b = int(c.evaluate(read) or -1)
    dt = time.time() - t0
    if a < 0 or b < 0 or dt <= 0:
        return None
    return (b - a) / dt


def capture(c, path):
    png = base64.b64decode(c.call("Page.captureScreenshot", format="png")["data"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(png)
    return path


def run_backend(c, want, log, settle):
    """Store `want`, reload, wait, and record what happened.

    No try/finally any more: there is nothing to unwind. The setting is a
    localStorage key that is MEANT to persist - that is the whole mechanism -
    and `main` puts the original back at the end.
    """
    log.info("--- %s ---", want)
    if not force_renderer(c, want):
        log.info("could not store renderMode=%s - skipping this backend", want)
        return {"requested": want, "info": {}, "fps": None, "frame": None}

    c.call("Page.reload", ignoreCache=False)
    time.sleep(2.0)
    info = wait_for_ruffle(c)
    if not info.get("live"):
        log.info("no canvas came up: %s", info.get("where"))
        log.info("(if this is character select, the SWF has not loaded - "
                 "the operator must be signed in)")
        return {"requested": want, "info": info, "fps": None, "frame": None}

    # Let it settle before capturing: a frame grabbed mid-load compares the
    # loading screen against a game screen and reports a huge difference that
    # has nothing to do with the renderer.
    time.sleep(settle)
    fps = measure_fps(c)
    frame = capture(c, os.path.join(OUT, f"{want.replace('-', '_')}.png"))
    msg, _ = browser.describe_renderer(info)
    log.info("%s", msg)
    log.info("rAF %s fps, frame -> %s",
             f"{fps:.1f}" if fps else "?", os.path.relpath(frame, ROOT))

    # ASKING IS NOT GETTING. Report the mismatch rather than labelling the
    # frame with a backend it may not be using.
    if info.get("requested") and info["requested"] != want:
        log.info("NOTE: asked for %s but the player loaded with %s",
                 want, info["requested"])
    return {"requested": want, "info": info, "fps": fps, "frame": frame}


def score_frames(results, log):
    """Score every template on every captured frame and report what MOVED.

    A threshold is only safe across backends if the positives stay above it and
    the negatives stay below. Both directions matter: a negative that RISES on
    another backend is a false-positive waiting to happen.
    """
    frames = {r["requested"]: r["frame"] for r in results if r.get("frame")}
    if len(frames) < 2:
        log.info("need at least two captured frames to compare")
        return
    perceive.clear_search_band()
    cfg = json.load(open(os.path.join(ROOT, "Configs/mission.json")))
    tpls = perceive.load_templates(cfg, _Log())

    grays = {k: cv2.cvtColor(cv2.imread(v), cv2.COLOR_BGR2GRAY)
             for k, v in frames.items()}
    base = next(iter(grays))

    print()
    print(f"  raw pixel difference from {base}:")
    for k, g in grays.items():
        if k == base:
            continue
        a, b = grays[base], g
        if a.shape != b.shape:
            print(f"    {k:<12} DIFFERENT SIZE {a.shape} vs {b.shape}")
            continue
        d = cv2.absdiff(a, b)
        print(f"    {k:<12} mean {d.mean():6.2f}   frac>16 {float((d > 16).mean()):.3f}")

    print()
    print(f"  {'template':<24}" + "".join(f"{k:>14}" for k in grays) + "   verdict")
    moved = []
    for name, tp in sorted(tpls.items()):
        scores = {}
        for k, g in grays.items():
            try:
                _, conf = find(g, tp)
            except Exception:
                conf = float("nan")
            scores[k] = conf
        vals = [v for v in scores.values() if v == v]
        if not vals:
            continue
        spread = max(vals) - min(vals)
        thr = tp.threshold
        # Does the DECISION change anywhere? That is what actually matters.
        decisions = {k: (v >= thr) for k, v in scores.items()}
        flipped = len(set(decisions.values())) > 1
        if flipped or spread > 0.05:
            moved.append((name, spread, flipped))
            verdict = "DECISION FLIPS" if flipped else f"spread {spread:.3f}"
            print(f"  {name:<24}" + "".join(f"{scores[k]:>14.3f}" for k in grays)
                  + f"   {verdict}")
    print()
    if not moved:
        print("  no template moved by more than 0.05 and no decision changed -")
        print("  one calibration serves these backends.")
    else:
        flips = [m for m in moved if m[2]]
        print(f"  {len(moved)} template(s) moved > 0.05; "
              f"{len(flips)} changed a DECISION.")
        if flips:
            print("  -> thresholds are NOT portable across these backends:")
            for n, s, _ in flips:
                print(f"       {n} (spread {s:.3f})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--only", action="append", choices=BACKENDS,
                    help="test just this backend (repeatable)")
    ap.add_argument("--settle", type=float, default=6.0,
                    help="seconds to let the game settle before capturing")
    ap.add_argument("--score-only", action="store_true",
                    help="re-score frames already in ref/auto/renderer")
    a = ap.parse_args()
    log = _Log()

    if a.score_only:
        results = [{"requested": f[:-4].replace("_", "-"),
                    "frame": os.path.join(OUT, f)}
                   for f in sorted(os.listdir(OUT)) if f.endswith(".png")]
        print(f"scoring {len(results)} saved frame(s)")
        score_frames(results, log)
        return 0

    if not browser.cdp_ready(a.port):
        print(f"nothing serving CDP on {a.port}. Start the bot first.")
        return 2
    t = cdp.find_page_target(port=a.port, timeout=20)
    c = cdp.CDP(t["webSocketDebuggerUrl"])

    before = browser.renderer_info(c)
    msg, _ = browser.describe_renderer(before)
    print(f"starting from: {msg}")
    # Restore whatever the GAME had stored, which may be nothing at all - in
    # which case the site falls back to its own default and there is nothing
    # to put back.
    _g = browser.read_game_settings(c)
    original = (_g.get("settings") or {}).get("renderMode")
    if not original:
        print(f"  (renderMode was unset; the site defaults to "
              f"{_g.get('default_render')})")

    wanted = a.only or BACKENDS
    results = []
    try:
        for want in wanted:
            results.append(run_backend(c, want, log, a.settle))
    finally:
        # PUT IT BACK. Leaving the game on canvas2d would quietly slow every
        # later session and invalidate every threshold in the project.
        if original:
            print(f"\nrestoring {original}")
            force_renderer(c, original)
            try:
                c.call("Page.reload", ignoreCache=False)
                time.sleep(3.0)
                m, _ = browser.describe_renderer(wait_for_ruffle(c, 60))
                print(f"  {m}")
            except Exception as e:
                print(f"  could not confirm the restore: {e}")

    print()
    print("summary")
    for r in results:
        info = r.get("info") or {}
        print(f"  asked {r['requested']:<12} got {str(info.get('live')):<10} "
              f"fps {('%.1f' % r['fps']) if r.get('fps') else '-':>6}")
    score_frames(results, log)
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
