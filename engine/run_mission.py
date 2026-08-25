#!/usr/bin/env python3
"""Mission farming entry point.

DRY RUN BY DEFAULT. Nothing is clicked unless you pass --live, matching bot.py's
convention — the first live run of the observation loop was what showed that
perception without a policy does nothing useful, and the same caution applies in
reverse here: a policy without verified perception clicks the wrong things.

    .venv/bin/python engine/run_mission.py                  # dry run, 1 mission
    .venv/bin/python engine/run_mission.py --preflight      # just list what's missing
    .venv/bin/python engine/run_mission.py --live --repeat 5

Before the first live run:
  1. --preflight and cut every template it names (see mission.REQUIRED_TEMPLATES)
  2. set mission.grade in Configs/mission.json — there is no default
  3. measure mission.traversal_click, or the mission will stall on the walk
  4. confirm that clicking a target ring slot actually selects a target; that is
     inferred from the reference bot, NOT yet verified on our client
"""
import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import browser
from act import Actor, Controls
from capture import Capture
from cdp import CDP, find_page_target, CDPError
from gate import Gate
from mission import MissionRunner, MissionOutcome, preflight
from perceive import Template

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setup_logging(path):
    fmt = "%(asctime)s %(levelname)-5s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%H:%M:%S",
                        handlers=[logging.FileHandler(path, mode="a"),
                                  logging.StreamHandler(sys.stdout)])
    return logging.getLogger("mission")


def load_templates(log):
    """Every template in tpl/, at its configured or default threshold.

    Underscore-prefixed files are excluded by convention — those are known-bad
    or work-in-progress crops (`_weak_battle_label`, `_weak_hunting_house_label`)
    that CLAUDE.md records as unusable, being semi-transparent art over animated
    scenery.
    """
    out = {}
    d = os.path.join(ROOT, "tpl")
    for f in sorted(os.listdir(d)):
        if not f.endswith(".png") or f.startswith("_"):
            continue
        n = f[:-4]
        out[n] = Template(n, os.path.join(d, f), threshold=0.88)
    log.info("loaded %d templates", len(out))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "Configs/mission.json"))
    ap.add_argument("--live", action="store_true",
                    help="ACTUALLY CLICK (default is a dry run)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="how many missions to attempt")
    ap.add_argument("--preflight", action="store_true",
                    help="report missing templates and exit")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--no-pin", action="store_true")
    args = ap.parse_args()

    os.makedirs(os.path.join(ROOT, "run"), exist_ok=True)
    log = setup_logging(os.path.join(ROOT, "run/mission.log"))
    cfg = json.load(open(args.config))

    templates = load_templates(log)

    missing = preflight(templates, log)
    if args.preflight:
        if not missing:
            log.info("preflight OK: every required template is present")
        return 1 if missing else 0
    if missing and args.live:
        log.error("refusing to run live with %d missing template(s). "
                  "Run --preflight for the list, or use the dry run.", len(missing))
        return 2
    if missing:
        log.warning("DRY RUN with %d missing template(s) — states that depend on "
                    "them will read as 'unknown'", len(missing))

    dry = not args.live
    log.info("=" * 68)
    log.info("MODE: %s", "DRY RUN - nothing will be clicked" if dry else "*** LIVE ***")
    log.info("grade=%s  repeat=%d", cfg.get("mission", {}).get("grade"), args.repeat)
    log.info("=" * 68)

    if not browser.cdp_ready(args.port):
        log.info("no CDP on %d - launching a dedicated-profile Chrome", args.port)
        try:
            browser.launch(cfg.get("target", {}).get("game_url", ""),
                           profile_dir=os.path.join(ROOT, "run/chrome-profile"),
                           port=args.port)
        except Exception as e:
            log.error("launch failed: %s", e)
            return 2

    try:
        target = find_page_target(port=args.port,
                                 url_contains=cfg.get("target", {})
                                 .get("url_contains", "ninjasaga"), timeout=30)
    except CDPError as e:
        log.error("%s", e)
        return 2

    c = CDP(target["webSocketDebuggerUrl"])
    c.call("Page.enable")

    if not args.no_pin:
        g = cfg.get("geometry", {}).get("viewport", {})
        log.info("pinning viewport (Emulation, NOT css resize): %s",
                 browser.pin_viewport(c, g.get("width", 960), g.get("height", 839),
                                      g.get("deviceScaleFactor", 2)))

    cap = Capture(c)
    log.info("capture: viewport=%s dpr=%s", cap.viewport, cap.dpr)

    t = cfg.get("timing", {})
    ctl = Controls(os.path.join(ROOT, "run/bot.control"), log)
    actor = Actor(c, cap, log, dry_run=dry,
                  click_delay=tuple(t.get("click_delay", (0.18, 0.55))),
                  jitter_px=t.get("jitter_px", 3),
                  post_click=tuple(t.get("post_click", (0.4, 1.1))))
    gate = Gate(cap, log, controls=ctl,
                poll_interval=t.get("poll_interval", 0.25))

    tally = {}
    try:
        for i in range(args.repeat):
            if not ctl.wait_if_paused():
                log.warning("stop requested before mission %d", i + 1)
                break
            log.info("--- mission %d/%d ---", i + 1, args.repeat)
            runner = MissionRunner(gate, actor, cap, templates, cfg, log,
                                   controls=ctl)
            outcome, stats = runner.run()
            tally[outcome] = tally.get(outcome, 0) + 1
            log.info("mission %d -> %s  %s", i + 1, outcome, stats)
            if outcome in (MissionOutcome.STOPPED, MissionOutcome.LOCKED):
                log.warning("halting the batch: %s", outcome)
                break
    finally:
        log.info("=" * 68)
        log.info("tally: %s", tally or "nothing ran")
        log.info("log: run/mission.log")
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
