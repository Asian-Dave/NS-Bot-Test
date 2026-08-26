#!/usr/bin/env python3
"""Play a mission to the end, capturing every distinct screen on the way.

Purpose: the last two templates the mission runner needs — `result_panel` (the
mid-mission Victory! panel) and `mission_success` (the end-of-mission panel) —
cannot be minted from the game's SWF bitmaps, because both are vector-drawn.
They have to come from live frames, and a mission has to actually finish to
produce them.

This is a capture harness, not the bot. It does the minimum needed to keep a
mission moving and saves a frame whenever the screen meaningfully changes:

  * command bar present  -> click Attack. Measured: Attack alone RESOLVES the
    turn on our client, with no separate target click, so this is enough to
    finish a fight. It is slow (~8 percentage points per hit) but it is the only
    action verified to deal damage.
  * command bar absent   -> click the canvas centre, which advances cutscenes and
    dismisses result panels ("click anywhere to continue").

Deliberately narrow, for safety:
  * The ONLY coordinates it clicks are the Attack button (derived from the
    command-bar anchor, never hardcoded) and the canvas centre. It never clicks a
    bar-derived point, and never anything in the HUD row that holds the token `+`
    controls.
  * It records the token count region every frame so a spend would be visible
    after the fact.

Frames land in ref/auto/panels/. Dedup is by mean absolute difference, so the
continuously-animating battle background does not produce thousands of files —
CLAUDE.md notes frame-differencing is useless as an EVENT signal here, but it is
fine as a "is this a different screen" filter with a high threshold.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from act import Actor
from capture import Capture
from cdp import CDP, find_page_target
from geometry import BattleGeometry
from perceive import Template


class _Log:
    def info(self, m, *a):
        print(("  " + m) % a if a else "  " + m, flush=True)
    warning = error = info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=12.0)
    ap.add_argument("--out", default=os.path.join(ROOT, "ref/auto/panels"))
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--diff", type=float, default=6.0,
                    help="mean abs frame diff to count as a NEW screen")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    log = _Log()

    t = find_page_target(port=a.port, url_contains="ninjasaga", timeout=20)
    c = CDP(t["webSocketDebuggerUrl"])
    c.call("Page.enable")
    cap = Capture(c)
    actor = Actor(c, cap, log, dry_run=False,
                  click_delay=(0.08, 0.18), post_click=(0.15, 0.35))
    ch = Template("charge_btn", os.path.join(ROOT, "tpl/charge_btn.png"), threshold=0.70)
    do = Template("dodge_btn", os.path.join(ROOT, "tpl/dodge_btn.png"), threshold=0.70)
    # A result panel is NOT dismissed by clicking anywhere - it needs its green
    # check. Measured: 11 centre-clicks on a Victory panel did nothing at all,
    # because the centre lands on the panel body. The check is the only hit area.
    rp = Template("result_panel", os.path.join(ROOT, "tpl/result_panel.png"),
                  threshold=0.85)
    gc = Template("green_check", os.path.join(ROOT, "tpl/mission_start.png"),
                  threshold=0.80)
    # same glyph at 3 sizes: detail 1.00, Victory 1.18, Mission Success 1.84
    gc.scales = [round(0.95 + i * 0.05, 2) for i in range(21)]   # 0.95..1.95
    # Scale is EXACTLY 1.0 at the pinned 1720x720 viewport (stage 960x720, so
    # Ruffle's min(vw/960, vh/720) is height-limited at 1.0) - verified live. So
    # no sweep, and crop before matching: the full frame is 3440x1440 and
    # matching it was costing ~30s per loop, which dominated everything.
    EXACT = [1.0]

    prev = None
    saved = 0
    attacks = 0
    advances = 0
    deadline = time.time() + a.minutes * 60
    print(f"capturing for up to {a.minutes} min -> {a.out}", flush=True)

    while time.time() < deadline:
        bgr = cap.frame(gray=False)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        h, w = bgr.shape[:2]

        # save a frame whenever the screen changed materially
        small = cv2.resize(gray, (w // 8, h // 8), interpolation=cv2.INTER_AREA)
        if prev is None or float(np.abs(small.astype(np.int16) -
                                       prev.astype(np.int16)).mean()) > a.diff:
            saved += 1
            p = os.path.join(a.out, f"f{saved:04d}.png")
            cv2.imwrite(p, bgr)
            print(f"  [{time.strftime('%H:%M:%S')}] saved {os.path.basename(p)}",
                  flush=True)
            prev = small

        # Crops, in captured px. The canvas is x 760..2680 at this viewport, and
        # each element only ever appears in one band, so searching the whole
        # frame is pure waste.
        PX, PY = 1100, 200            # result panel band origin
        CX, CY = 1200, 640            # command bar band origin
        panel_band = gray[PY:PY + 700, PX:PX + 1500]
        cmd_band = gray[CY:CY + 500, CX:CX + 1100]

        from perceive import find as _find
        # Result panel FIRST. It draws over the command bar, so testing the bar
        # first would read a finished fight as "my turn".
        rpm, rpc = _find(panel_band, rp)
        if rpm.found:
            gcm, gcc = _find(panel_band, gc)
            if gcm.found:
                actor.click_pixel(gcm.center[0] + PX, gcm.center[1] + PY,
                                  why=f"dismiss result panel via green check "
                                      f"(panel {rpc:.3f}, check {gcc:.3f})")
            else:
                log.info("result panel up (%.3f) but green check not located "
                         "(best %.3f) - not guessing a click", rpc, gcc)
            time.sleep(1.2)
            continue

        geo = BattleGeometry.locate(cmd_band, ch, do, scales=EXACT)
        if geo is not None:
            geo.anchor = (geo.anchor[0] + CX, geo.anchor[1] + CY)
        if geo is not None:
            # our turn: Attack. Point derived from the anchor, never hardcoded.
            attacks += 1
            actor.click_pixel(*geo.cmd("AT"), why=f"Attack #{attacks}")
            time.sleep(0.8)
        else:
            # no command bar: cutscene, result panel, traversal, or loading.
            # The canvas centre is the safe universal "continue" click - far from
            # the HUD row that holds the token + controls.
            advances += 1
            actor.click_pixel(w // 2, int(h * 0.62), why=f"advance #{advances}")
            time.sleep(0.7)

    print(f"\ndone: {saved} frames, {attacks} attacks, {advances} advances", flush=True)
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
