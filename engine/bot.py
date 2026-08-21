#!/usr/bin/env python3
"""Dry-run observation loop.

Purpose: replace round-trip-per-fact exploration with one log file. It detects
state, scores EVERY template every cycle, tracks rounds / HP / cooldowns, and by
default clicks NOTHING.

It also sweeps template scale and reports the peak scale per template. That is
deliberate: two template sets were captured at different canvas geometries, and
rather than guess the relationship, the loop measures it. matchTemplate is not
scale-invariant, and text templates lose ~0.4 confidence at 8% scale error, so
knowing the true peak matters more than any assumption.

Usage:
    .venv/bin/python engine/bot.py                 # dry run, attach or launch
    .venv/bin/python engine/bot.py --cycles 40
    .venv/bin/python engine/bot.py --no-pin        # leave viewport alone
"""
import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

import browser
from cdp import CDP, find_page_target, CDPError
from capture import Capture
from perceive import Template, find
import combat
from act import Actor, Controls

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCALES = [round(0.40 + i * 0.02, 2) for i in range(0, 36)]   # 0.40 .. 1.10


def setup_logging(path):
    fmt = "%(asctime)s %(levelname)-5s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%H:%M:%S",
                        handlers=[logging.FileHandler(path, mode="w"),
                                  logging.StreamHandler(sys.stdout)])
    return logging.getLogger("bot")


def load_templates(cfg, log):
    """Load every template the config names, skipping any that are absent."""
    out, missing = {}, []
    for name, spec in cfg.get("templates", {}).items():
        if name.startswith("_"):
            continue
        path = os.path.join(ROOT, spec["path"])
        if not os.path.exists(path):
            missing.append(name)
            continue
        out[name] = Template(name, path, threshold=spec.get("threshold", 0.88))
    # anything in tpl/ that the config forgot is still worth scoring
    tpl_dir = os.path.join(ROOT, "tpl")
    for f in sorted(os.listdir(tpl_dir)):
        if not f.endswith(".png") or f.startswith("_"):
            continue
        n = f[:-4]
        if n not in out:
            out[n] = Template(n, os.path.join(tpl_dir, f), threshold=0.88)
    if missing:
        log.warning("config names %d template(s) with no file: %s",
                    len(missing), ", ".join(missing))
    log.info("loaded %d templates", len(out))
    return out


def score_all(frame_gray, templates, scales):
    """Best confidence + peak scale for every template. This is the payload."""
    rows = []
    for name, t in sorted(templates.items()):
        best = (-1.0, None, None)
        for s in scales:
            th, tw = int(t.h * s), int(t.w * s)
            if th < 6 or tw < 6 or th > frame_gray.shape[0] or tw > frame_gray.shape[1]:
                continue
            small = cv2.resize(t.gray, (tw, th),
                               interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
            res = cv2.matchTemplate(frame_gray, small, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if mx > best[0]:
                best = (float(mx), s, (loc[0] + tw // 2, loc[1] + th // 2))
        rows.append((name, best[0], best[1], best[2], t.threshold))
    return rows


def identify_state(scored, cfg):
    """Classify the frame. Every threshold below is MEASURED, not guessed - see
    the discrimination matrix in CLAUDE.md.

    Two hard-won rules encoded here:

    1. The persistent shell (lobby_logo, nav_*) is NOT a state discriminator.
       Those score 0.87-0.98 identically in the lobby, over a popup, AND in
       combat. They only separate "inside the game" from "loading".
    2. Require TWO corroborating templates for combat. Individually the command
       buttons vary (charge 0.949, dodge 0.918, run 0.940, attack only 0.791);
       together they are unambiguous.

    `click_to_continue` is deliberately NOT used: measured 0.642-0.849 across
    unrelated states, it false-fires on combat and cannot gate anything.
    """
    c = {n: conf for n, conf, _, _, _ in scored}
    g = lambda n: c.get(n, 0.0)

    shell = max(g("lobby_logo"), g("nav_talent"), g("nav_option"))

    # loading: the shell is hidden behind a black interstitial
    if g("loading_text") >= 0.80 and shell < 0.60:
        return "loading"

    # daily reward popup: the day strip is highly distinctive
    if g("day_claimed_check") >= 0.90 or g("day_current_pointer") >= 0.90:
        return "daily_reward_popup"

    # combat: two of the four command buttons must agree
    cmd = sorted([g("charge_btn"), g("dodge_btn"), g("run_btn"), g("attack_btn")],
                 reverse=True)
    if cmd[0] >= 0.85 and cmd[1] >= 0.85:
        return "combat"

    # a dismissible modal of some kind
    if g("close_popup_x") >= 0.90 or g("close_popup_x_menu") >= 0.88:
        return "popup"

    # character select: strong, verified discriminator (0.33-0.35 elsewhere)
    if g("character_select") >= 0.85:
        return "character_select"

    if shell >= 0.85:
        # No positive lobby anchor exists yet - the village labels are
        # semi-transparent over animated art and unusable. Until one is cut,
        # lobby is defined negatively: shell present, nothing else matched.
        return "lobby_or_shell"

    return "unknown"


# Templates that must NEVER be a click target, at any confidence, in any state.
# `delete_btn` sits on the same row as Play and destroys a character permanently.
# Enforced in decide_action AND again at the click site (defence in depth).
NEVER_CLICK = {"delete_btn", "claim_daily", "wish_btn", "spin_btn"}


def decide_action(state, scored, ctx=None):
    return _guard(_decide(state, scored, ctx))


def _decide(state, scored, ctx=None):
    """Map a recognised state to an intended action.

    Returns {action, target, at, reason}. `action` is one of:
      click / wait / idle / abort / halt / none

    Perception without a policy does nothing useful, which is exactly what the
    first live run showed. Guard rails encoded here:

      * once-per-day resources (Claim / Wish / SPIN) are NEVER auto-clicked -
        they are irreversible and single-use, so they need explicit opt-in.
      * LOGGED_OUT / LOGIN_FORM halt and notify. The bot never authenticates.
      * a stalled or regenerating fight aborts via Run rather than grinding a
        fight it cannot win (observed: enemy HP recovering 43.0 -> 47.2).
    """
    ctx = ctx or {}
    c = {n: (conf, loc) for n, conf, _s, loc, _t in scored}
    def at(name, thr=0.85):
        v = c.get(name)
        return v[1] if v and v[0] >= thr else None

    if state == "loading":
        return {"action": "wait", "reason": "loading interstitial; 8-30s observed"}

    if state == "character_select":
        # Two-step: select a slot, THEN Play. Play only exists after selection,
        # so its presence is what tells us which step we are on.
        play = at("play_btn")
        if play:
            return {"action": "click", "target": "play_btn", "at": play,
                    "reason": "character selected; enter the game"}
        p = at("char_slot_level")
        if p:
            # the Level label sits inside the card; click the card, not the label
            return {"action": "click", "target": "character_card",
                    "at": (p[0] - 40, p[1] - 20),
                    "reason": "select character; Play appears after"}
        return {"action": "none", "reason": "no occupied slot found"}

    if state == "daily_reward_popup":
        # Deliberately dismiss rather than Claim.
        p = at("close_popup_x")
        return ({"action": "click", "target": "close_popup_x", "at": p,
                 "reason": "dismiss; Claim is once-per-day and needs opt-in"}
                if p else {"action": "none", "reason": "claim guarded, no dismiss found"})

    if state == "popup":
        for t in ("close_popup_x", "close_popup_x_menu", "close_popup_x_large",
                  "close_popup_back_arrow"):
            p = at(t)
            if p:
                return {"action": "click", "target": t, "at": p,
                        "reason": "drain popup queue (login queues four)"}
        return {"action": "none", "reason": "popup with no known dismiss control"}

    if state == "combat":
        if ctx.get("watchdog") in ("stalled", "regenerating"):
            p = at("run_btn", 0.80)
            return {"action": "abort", "target": "run_btn", "at": p,
                    "reason": f"watchdog={ctx['watchdog']}: fight not progressing"}
        if ctx.get("my_turn"):
            p = at("attack_btn", 0.75)      # peaks ~0.79, so a lower gate
            return ({"action": "click", "target": "attack_btn", "at": p,
                     "reason": "player turn"} if p else
                    {"action": "none", "reason": "turn open but no action button located"})
        return {"action": "wait", "reason": "enemy turn"}

    if state in ("logged_out", "login_form"):
        return {"action": "halt", "reason": "NOT LOGGED IN - will not authenticate"}

    if state == "lobby_or_shell":
        return {"action": "idle", "reason": "lobby; no behaviour configured yet"}

    return {"action": "none", "reason": f"unhandled state {state!r}"}


def _guard(decision):
    """Refuse any decision that targets a forbidden template."""
    if decision.get("target") in NEVER_CLICK:
        return {"action": "none",
                "reason": f"BLOCKED: {decision['target']} is on the never-click list"}
    return decision


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "configs/daily_reward.json"))
    ap.add_argument("--cycles", type=int, default=25)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--no-pin", action="store_true")
    ap.add_argument("--live", action="store_true", help="ACTUALLY CLICK (default is dry run)")
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()

    os.makedirs(os.path.join(ROOT, "run"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "ref/auto"), exist_ok=True)
    log = setup_logging(os.path.join(ROOT, "run/bot.log"))

    cfg = json.load(open(args.config))
    dry = not args.live
    log.info("=" * 68)
    log.info("MODE: %s", "DRY RUN - nothing will be clicked" if dry else "*** LIVE ***")
    log.info("=" * 68)

    # --- attach, or launch our own dedicated-profile Chrome -----------------
    if not browser.cdp_ready(args.port):
        log.info("no CDP on %d - launching a dedicated-profile Chrome", args.port)
        try:
            browser.launch("https://ninjasaga.cc/play",
                           profile_dir=os.path.join(ROOT, "run/chrome-profile"),
                           port=args.port)
        except Exception as e:
            log.error("launch failed: %s", e)
            return 2
    else:
        log.info("attaching to existing CDP on %d", args.port)

    try:
        target = find_page_target(port=args.port, url_contains="ninjasaga", timeout=30)
    except CDPError as e:
        log.error("%s", e)
        log.error("Is the game open in the Chrome that owns port %d?", args.port)
        return 2

    c = CDP(target["webSocketDebuggerUrl"])
    c.call("Page.enable")

    if not args.no_pin:
        g = cfg.get("geometry", {}).get("viewport", {})
        w, h, s = g.get("width", 960), g.get("height", 839), g.get("deviceScaleFactor", 2)
        log.info("pinning viewport %dx%d @ dsf %s (Emulation, NOT css resize)", w, h, s)
        log.info("  -> %s", browser.pin_viewport(c, w, h, s))

    cap = Capture(c)
    log.info("capture: viewport=%s dpr=%s", cap.viewport, cap.dpr)

    templates = load_templates(cfg, log)
    actor = Actor(c, cap, log, dry_run=dry)
    ctl = Controls(os.path.join(ROOT, "run/bot.control"), log)

    cds = cfg.get("combat", {}).get("cooldowns", {}).get("rounds_per_slot", {})
    tracker = combat.CooldownTracker(cds)
    watchdog = combat.DamageWatchdog()
    counters = {"cycles": 0, "states": {}, "unknown": 0}
    seen_states, last_state = set(), None

    for i in range(args.cycles):
        if not ctl.wait_if_paused():
            log.warning("stop requested via control file")
            break
        counters["cycles"] += 1
        try:
            bgr = cap.frame(gray=False)
        except Exception as e:
            log.error("capture failed: %s", e)
            break
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        scored = score_all(gray, templates, SCALES)
        state = identify_state(scored, cfg)
        counters["states"][state] = counters["states"].get(state, 0) + 1
        if state == "unknown":
            counters["unknown"] += 1

        log.info("-- cycle %d/%d  state=%s", i + 1, args.cycles, state)
        for name, conf, scale, loc, thr in scored:
            if conf >= 0.60:                     # suppress pure noise
                flag = "HIT " if conf >= thr else "    "
                log.info("   %s%-26s conf=%.3f  peak_scale=%.2f  at=%s",
                         flag, name, conf, scale, loc)

        # save one frame per newly-seen state, for re-cutting templates later
        if state not in seen_states:
            seen_states.add(state)
            p = os.path.join(ROOT, f"ref/auto/state_{state}.png")
            cv2.imwrite(p, bgr)
            log.info("   [saved first frame of '%s' -> %s]", state, p)

        if state == "combat":
            bars = combat.find_enemy_bars(bgr, x0=int(bgr.shape[1] * 0.55),
                                          x1=bgr.shape[1] - 4, y0=0,
                                          y1=int(bgr.shape[0] * 0.75))
            log.info("   enemy bars: %s",
                     [f"y={y} {f:.1f}%" for y, f in bars] or "none found")
            my_turn = combat.is_my_turn(gray, templates["attack_btn"]) \
                if "attack_btn" in templates else None
            log.info("   my_turn=%s  round=%d", my_turn, tracker.round)
            if bars:
                v = watchdog.observe(max(f for _, f in bars))
                log.info("   watchdog=%s history=%s", v,
                         [f"{h:.1f}" for h in watchdog.history[-6:]])
                if v in ("stalled", "regenerating"):
                    log.warning("   >> would take RUN (%s) - fight is not progressing", v)
            if my_turn:
                tracker.next_round()
                log.info("   >> would act this turn (dry run: no click)")

        if state != last_state:
            log.info("   [transition %s -> %s]", last_state, state)
            last_state = state
        time.sleep(args.interval)

    log.info("=" * 68)
    log.info("cycles=%d  unknown=%d", counters["cycles"], counters["unknown"])
    log.info("state histogram: %s", counters["states"])
    log.info("frames saved for template re-cutting: ref/auto/")
    log.info("full log: run/bot.log")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
