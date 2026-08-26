#!/usr/bin/env python3
"""Local web dashboard — the control panel, zero dependencies beyond the engine.

Layout mirrors the reference product: live game view on the left, controls and
live counters on the right. The difference is where the frames come from.

Why frames arrive over CDP rather than an iframe: a page on another origin cannot
read a cross-origin iframe's DOM or canvas (same-origin policy), and no CORS
header changes that. So the game is *not* embedded - we poll
`Page.captureScreenshot` and paint the result. Identical UX, none of the sandbox
problems, and it keeps the OpenCV perception stack.

Why polling instead of `Page.startScreencast`: our CDP client is synchronous and
discards unmatched messages, so streamed frame *events* would be dropped.
Polling is honest and works today; screencast would need event handling in cdp.py.

    .venv/bin/python engine/dashboard.py            # bot + UI
    .venv/bin/python engine/dashboard.py --no-bot   # UI only (layout check)
    ->  http://127.0.0.1:8770
"""
import argparse
import json
import os
import sys
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2

import browser
import combat
import overlay
import resume
from cdp import CDP, find_page_target, CDPError
from capture import Capture
from perceive import Template
from act import Actor
from bot import score_all, identify_state, load_templates, decide_action, NEVER_CLICK, SCALES

# At the CANONICAL geometry (960x839 @ dsf2 -> 1920x1678 native) templates match
# at scale ~1.0, because that is the geometry they were cut at. The old 0.46 band
# came from 1568-wide screenshots, NOT native frames - reusing it here put the
# true peak outside the band and made every template score wrong. The full 36-step sweep costs 16.05s/cycle vs 0.40s locked (40x) -
# it is a calibration tool, never a runtime one. Enable it with --sweep.
# Hot-path scales. MEASURED on a live pinned frame: every template that hit
# peaked at exactly 1.00, and a full 36-step sweep cost 52,000 ms against
# 1,568 ms at native scale. We pin the viewport with
# Emulation.setDeviceMetricsOverride, so exactly one geometry ever occurs and
# searching scale is searching for something that cannot vary.
# Use --sweep when you actually want to measure the peak (calibration), never
# in the loop.
SCALES_FAST = [1.00]
# Templates cut natively from a canonical frame match at 1.00 exactly. Three
# were re-cut by upscaling a reference JPEG, which carries ~3% scale error and
# peaks nearer 1.03 - the lobby anchor scored 0.902 there while missing at 1.00.
# Drop back to [1.00] once those are re-cut from live captures.


class Shared:
    """Everything the UI reads. One lock, coarse and simple."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frame_jpeg = None
        self.state = "not started"
        self.scored = []
        self.enemy_bars = []
        self.my_turn = None
        self.watchdog = "-"
        self.round = 0
        self.cycle = 0
        self.counters = {"cycles": 0, "unknown": 0, "combat": 0, "popups": 0, "errors": 0}
        self.mode = "dry-run"
        self.status = "idle"
        self.score_ms = 0
        self.decision = {}
        self.cdp = None
        self.log = []

    def note(self, msg):
        with self.lock:
            self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
            del self.log[:-200]

    def snapshot(self):
        with self.lock:
            return {
                "state": self.state, "cycle": self.cycle, "round": self.round,
                "my_turn": self.my_turn, "watchdog": self.watchdog,
                "counters": dict(self.counters), "mode": self.mode,
                "status": self.status, "score_ms": self.score_ms,
                "decision": dict(self.decision),
                "enemy_bars": [{"y": y, "pct": round(p, 1)} for y, p in self.enemy_bars],
                "templates": [
                    {"name": n, "conf": round(c, 3), "scale": s, "thr": t,
                     "hit": bool(c >= t)}
                    for n, c, s, _loc, t in sorted(self.scored, key=lambda r: -r[1])[:14]
                ],
                "log": self.log[-40:],
            }


SH = Shared()
CONTROL = os.path.join(ROOT, "run/bot.control")
PANEL_W = 380
TASKS_PATH = os.path.join(ROOT, "run/tasks.json")

# The /emulator URL, held in memory ONLY.
#
# It carries a live session token (fb_at, fb_sig, fb_uid) and is time-signed
# (time, hash_time, _cb). CLAUDE.md is explicit: never persist it, never log it,
# never commit it. So it lives in this variable, is written into the embed page
# at serve time, and is redacted everywhere else. It is deliberately NOT put in
# tasks.json or any log line.
EMU_URL = None


def redact_url(u):
    """Origin + path, plus the NAMES of query params. Never their values."""
    if not u:
        return "(none)"
    m = re.match(r"(https?://[^/]+)(/[^?]*)\??(.*)", u)
    if not m:
        return "(unparseable)"
    names = sorted({p.split("=")[0] for p in m.group(3).split("&") if p})
    return f"{m.group(1)}{m.group(2)}" + (f" params={names}" if names else "")

# What the bot is allowed to DO, as opposed to which screens it may click on.
# Mirrors the reference bot's BotSequence idea, but lists only what we have
# actually implemented — a toggle for something that cannot run is a lie.
DEFAULT_TASKS = {
    "resume_to_lobby": True,
    "farm_missions": False,
    "tp_kekkai": False,
}

DEFAULT_OPTIONS = {
    "mission_grade": "A",
    "rotation": "AT",
    "closing_action": "AT",
    "watchdog_stall_turns": 3,
    "max_battles": 25,
}

# Tasks we cannot honestly offer yet, and why. Surfaced in the UI so the reason
# is visible at the point of use instead of buried in a doc.
def task_blockers(templates):
    import mission as mission_mod
    blocked = {}
    missing = [n for n in mission_mod.REQUIRED_TEMPLATES if n not in templates]
    if missing:
        blocked["farm_missions"] = (
            f"{len(missing)} template(s) missing: " + ", ".join(sorted(missing)[:4])
            + ("…" if len(missing) > 4 else ""))
    # TP: only the Kekkai family is understood, and it needs its own templates.
    tp_need = ("mission_room_entry", "special_tab", "tp_training_row",
               "tp_kekkai_row", "mission_success", "mission_start",
               "cutscene_continue", "page_next")
    tp_missing = [n for n in tp_need if n not in templates]
    if tp_missing:
        blocked["tp_kekkai"] = ("missing: " + ", ".join(sorted(tp_missing)[:4])
                                + ("…" if len(tp_missing) > 4 else ""))
    return blocked


class TaskState:
    def __init__(self):
        self.lock = threading.Lock()
        self.tasks = dict(DEFAULT_TASKS)
        self.options = dict(DEFAULT_OPTIONS)
        self.blocked = {}
        self.load()

    def load(self):
        try:
            with open(TASKS_PATH) as f:
                d = json.load(f)
            self.tasks.update(d.get("tasks", {}))
            self.options.update(d.get("options", {}))
        except Exception:
            pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(TASKS_PATH), exist_ok=True)
            with open(TASKS_PATH, "w") as f:
                json.dump({"tasks": self.tasks, "options": self.options}, f, indent=1)
        except Exception:
            pass

    def snapshot(self):
        with self.lock:
            return {"tasks": dict(self.tasks), "options": dict(self.options),
                    "blocked": dict(self.blocked)}

    def enabled(self, name):
        with self.lock:
            return bool(self.tasks.get(name)) and name not in self.blocked


TS = TaskState()
GAME_URL = ""          # set from config in main(); no host hardcoded here
RUNNER = None   # set at startup so the UI can arm/disarm states at runtime


class _Log:
    """Adapter so Actor can log into the dashboard's activity feed."""
    def info(self, m, *a):    SH.note(m % a if a else m)
    def warning(self, m, *a): SH.note("WARN " + (m % a if a else m))


class Runner(threading.Thread):
    daemon = True

    def __init__(self, cfg, port, pin, interval, scales, live=False, allow=(),
                 stream=False, use_overlay=True, embed=False, dash_port=8770):
        super().__init__()
        self.cfg, self.port, self.pin, self.interval = cfg, port, pin, interval
        self.scales = scales
        self.live = live
        # Live mode is opt-in PER STATE. An empty allowlist means dry everywhere,
        # so --live alone cannot arm anything by accident.
        self.allow = set(allow)
        self.last_act = {}          # state -> monotonic time of last action
        self.stream = stream
        self.use_overlay = use_overlay
        self.open_url = "http://127.0.0.1:%d/embed" % dash_port if embed else \
                        GAME_URL
        # CDP is origin-agnostic, so it can see and click the cross-origin game
        # iframe inside our own page. The PAGE's JS cannot - but it never needs to.
        self.match = "127.0.0.1" if embed else "ninjasaga"
        self.embed = embed
        self.dash_port = dash_port
        self.stop_flag = threading.Event()

    def run(self):
        SH.status = "connecting"
        SH.note("connecting to CDP…")
        try:
            if not browser.cdp_ready(self.port):
                SH.note("no CDP — launching dedicated-profile Chrome")
                browser.launch(self.open_url,
                               profile_dir=os.path.join(ROOT, "run/chrome-profile"),
                               port=self.port)
            if self.embed:
                # EMBED BOOTSTRAP.
                #
                # A cross-site iframe pointed at GAME_URL shows the logged-out
                # landing page: its session cookie is SameSite=Lax and is never
                # sent to ninjasaga.cc from a 127.0.0.1 page. MEASURED, not
                # assumed.
                #
                # The /emulator URL authenticates from its own query token
                # instead, and the server advertises no X-Frame-Options and no
                # CSP frame-ancestors, so it DOES load cross-site. So: attach to
                # the game tab, lift that URL, and frame it.
                #
                # It only exists once the game is actually running, so the tab
                # must already be past character select. If it is not, we say so
                # and fall back to streaming rather than framing a dead URL.
                global EMU_URL
                t0 = find_page_target(port=self.port, url_contains="ninjasaga",
                                      timeout=30)
                c0 = CDP(t0["webSocketDebuggerUrl"])
                c0.call("Page.enable")
                href = c0.evaluate("location.href") or ""
                if "/emulator" in href:
                    EMU_URL = href
                    SH.note("embed: lifted emulator URL " + redact_url(href))
                    c0.call("Page.navigate",
                            url="http://127.0.0.1:%d/embed" % self.dash_port)
                    c0.close()
                    time.sleep(3.0)
                else:
                    c0.close()
                    SH.note("embed: game tab is at " + redact_url(href) +
                            " — not /emulator yet, so there is no token URL to "
                            "frame. Falling back to streamed mode. Get into the "
                            "game once (the resume ladder will), then restart "
                            "with --embed.")
                    self.embed = False
                    self.match = "ninjasaga"

            t = find_page_target(port=self.port, url_contains=self.match, timeout=30)
            c = CDP(t["webSocketDebuggerUrl"])
            c.call("Page.enable")
        except Exception as e:
            SH.status = "cdp failed"
            SH.note(f"ERROR {e}")
            return

        if self.pin:
            g = self.cfg.get("geometry", {}).get("viewport", {})
            _w, _h = g.get("width", 960), g.get("height", 839)
            if self.embed:
                # Keep the game at its canonical 960x839 so templates still match
                # at 1.0; the extra width is purely for the control panel.
                _w, _h = _w + PANEL_W, max(_h, 880)
            browser.pin_viewport(c, _w, _h, g.get("deviceScaleFactor", 2))
            SH.note("viewport pinned (Emulation, not css resize)")

        SH.cdp = c
        # If a previous run injected the in-page panel, it is still in the DOM and
        # still occluding the frame we read. Clear it unless we want it.
        if not self.use_overlay:
            try:
                overlay.remove(c)
            except Exception:
                pass
        cap = Capture(c)
        actor = Actor(c, cap, _Log(), dry_run=not self.live)
        SH.note(f"capture viewport={cap.viewport} dpr={cap.dpr}")

        class _L:                          # load_templates wants a logger
            info = staticmethod(lambda *a: SH.note(a[0] % a[1:] if len(a) > 1 else a[0]))
            warning = info
        tpls = load_templates(self.cfg, _L)
        with TS.lock:
            TS.blocked = task_blockers(tpls)
        if TS.blocked:
            for k, why in TS.blocked.items():
                SH.note(f"task '{k}' unavailable: {why}")

        # The resume ladder. This is what makes the session progress instead of
        # re-deciding step one forever: it re-identifies the screen every pass
        # and takes the single step that advances it. See engine/resume.py.
        resumer = resume.Resumer(cap, actor, tpls, _Log(), never_click=NEVER_CLICK)

        wd = combat.DamageWatchdog()
        tracker = combat.CooldownTracker(
            self.cfg.get("combat", {}).get("cooldowns", {}).get("rounds_per_slot", {}))
        SH.status = "running"

        while not self.stop_flag.is_set():
            cmd = _read_control()
            if cmd == "stop":
                SH.status = "stopped by control file"
                SH.note("stop requested")
                break
            if cmd == "pause":
                SH.status = "paused"
                time.sleep(1.0)
                continue
            SH.status = "running"
            try:
                bgr = cap.frame(gray=False)
            except Exception as e:
                with SH.lock:
                    SH.counters["errors"] += 1
                SH.note(f"capture error: {e}")
                time.sleep(1.0)
                continue

            # Publish the frame FIRST. Previously it was only stored at the end
            # of a cycle, so a slow scoring pass made the live view 503 for the
            # whole cycle. The view must never depend on perception speed.
            if self.stream:
                # Optional. Encoding + polling is what made the web view lag, so
                # it is off unless explicitly requested.
                ok, jpg = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                if ok:
                    with SH.lock:
                        SH.frame_jpeg = jpg.tobytes()

            t_score = time.time()
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            scored = score_all(gray, tpls, self.scales)
            state = identify_state(scored, self.cfg)
            score_ms = int((time.time() - t_score) * 1000)

            bars, my_turn, verdict = [], None, "-"
            if state == "combat":
                bars = combat.find_enemy_bars(
                    bgr, x0=int(bgr.shape[1] * 0.55), x1=bgr.shape[1] - 4,
                    y0=0, y1=int(bgr.shape[0] * 0.75))
                if "attack_btn" in tpls:
                    my_turn = combat.is_my_turn(gray, tpls["attack_btn"])
                if bars:
                    verdict = wd.observe(max(p for _, p in bars))
                    if verdict in ("stalled", "regenerating"):
                        SH.note(f"WATCHDOG {verdict} — would take RUN")
                if my_turn:
                    tracker.next_round()

            # NAVIGATION: hand off to the resume ladder whenever we are not
            # already somewhere a task wants to work. `decide_action` is a
            # single-shot classifier — it maps one frame to one action and has no
            # notion of progressing through character select -> Play -> popups ->
            # lobby, which is exactly why the bot used to sit at the first step
            # forever. The ladder is re-entrant and idempotent, so calling it
            # once per cycle from any screen converges.
            # TP run. Deliberately BLOCKING: a mission takes minutes and has its
            # own gates, so the cycle loop pauses while it runs and the panel
            # stops updating until it finishes. That is honest about what is
            # happening rather than pretending to be responsive.
            if (TS.enabled("tp_kekkai") and state in ("lobby", "lobby_or_shell")
                    and self.live and "tp" in self.allow):
                SH.note("[tp] starting a Kekkai TP run (panel pauses until done)")
                try:
                    import tp as tp_mod
                    ok = (tp_mod.to_tp_list(actor, cap, _Log())
                          and tp_mod.pick_kekkai(actor, cap, _Log())
                          and tp_mod.run_one(cap, actor, _Log(), family="kekkai"))
                    SH.note(f"[tp] run {'completed' if ok else 'did not complete'}")
                except Exception as e:
                    SH.note(f"[tp] error: {type(e).__name__}: {e}")
                with TS.lock:
                    TS.tasks["tp_kekkai"] = False      # one run per tick-in
                TS.save()
                continue

            nav_states = ("character_select", "popup", "daily_reward_popup",
                          "loading", "unknown", "lobby_or_shell")
            if TS.enabled("resume_to_lobby") and state in nav_states:
                armed_nav = self.live and "navigate" in self.allow
                prev_dry = actor.dry_run
                actor.dry_run = not armed_nav
                try:
                    out, info = resumer.advance(gray)
                finally:
                    actor.dry_run = prev_dry
                SH.note(f"[resume] {out} {info}")
                decision = {"action": "none",
                            "target": info.get("step", ""),
                            "reason": f"resume ladder: {out} ({info.get('step','')})"}
            else:
                decision = decide_action(state, scored,
                                         {"my_turn": my_turn, "watchdog": verdict})
            act = decision.get("action")
            armed = self.live and state in self.allow
            if act in ("click", "abort") and decision.get("at"):
                # Rate-limit per state so a screen that does not change cannot be
                # click-spammed every cycle.
                import time as _t
                gap = _t.monotonic() - self.last_act.get(state, 0)
                if not armed:
                    SH.note(f"[{state}] would {act} {decision.get('target','')} "
                            f"at {decision.get('at')} - {decision['reason']}")
                elif gap < 4.0:
                    SH.note(f"[{state}] holding ({gap:.1f}s since last action)")
                else:
                    SH.note(f"[{state}] LIVE {act} {decision.get('target','')} "
                            f"at {decision.get('at')}")
                    tgt = decision.get("target", "")
                    if tgt in NEVER_CLICK:
                        SH.note(f"BLOCKED click on {tgt} (never-click list)")
                        continue
                    try:
                        actor.click_pixel(*decision["at"], why=tgt)
                        self.last_act[state] = _t.monotonic()
                    except Exception as e:
                        SH.note(f"click failed: {e}")
            elif act not in ("none", "idle", "wait"):
                SH.note(f"[{state}] {act} - {decision['reason']}")

            if self.use_overlay:
                try:
                    overlay.ensure(c)
                    overlay.update(
                        c, mode=SH.mode, live=armed, state=state, cycle=SH.cycle + 1,
                        score_ms=score_ms, action=decision.get("action"),
                        reason=decision.get("reason"), watchdog=verdict, bars=bars,
                        templates=[(n, cf, cf >= th)
                                   for n, cf, _s, _l, th in
                                   sorted(scored, key=lambda r: -r[1])[:6]])
                except Exception as e:
                    SH.note(f"overlay update failed: {e}")

            with SH.lock:
                SH.decision = decision
                SH.state, SH.scored = state, scored
                SH.score_ms = score_ms
                SH.enemy_bars, SH.my_turn, SH.watchdog = bars, my_turn, verdict
                SH.round, SH.cycle = tracker.round, SH.cycle + 1
                SH.counters["cycles"] += 1
                if state == "unknown":
                    SH.counters["unknown"] += 1
                if state == "combat":
                    SH.counters["combat"] += 1
                if state in ("popup", "daily_reward_popup"):
                    SH.counters["popups"] += 1
            time.sleep(self.interval)
        c.close()


def focus_emulator():
    """Navigate the tab to the game document itself, for a full clean render.

    The URL is read from the live iframe at runtime and used immediately. It
    carries a time-signed session token, so it is never stored, never logged and
    never echoed back to the UI - only the origin+path is reported.
    `location.replace` avoids putting the token in browser history.
    """
    c = SH.cdp
    if c is None:
        return {"ok": False, "error": "not connected yet"}
    try:
        js = """(() => {
          const f = [...document.querySelectorAll('iframe')].find(x => {
            try { return x.contentDocument &&
                          x.contentDocument.querySelector('ruffle-player'); }
            catch (e) { return false; } });
          if (!f) return JSON.stringify({ok:false, error:'no game iframe - are you logged in?'});
          const u = new URL(f.src, location.href);
          location.replace(f.src);
          return JSON.stringify({ok:true, where: u.origin + u.pathname});
        })()"""
        return json.loads(c.evaluate(js, await_promise=False))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _read_control():
    try:
        with open(CONTROL) as f:
            v = f.read().strip().lower()
        return v if v in ("run", "pause", "stop") else "run"
    except FileNotFoundError:
        return "run"


def _read(name, fallback):
    p = os.path.join(ROOT, "engine", name)
    return open(p).read() if os.path.exists(p) else fallback


PAGE = _read("dashboard.html", "<h1>missing dashboard.html</h1>")
EMBED = _read("embed.html", "<h1>missing embed.html</h1>")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                   # keep the console for bot output

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", PAGE.encode())
        if u.path == "/embed":
            # EMU_URL authenticates via its own query token, so it loads in a
            # cross-site iframe where GAME_URL (cookie-authenticated) would show
            # the logged-out landing page.
            page = EMBED.replace("__GAME_URL__", EMU_URL or GAME_URL)
            return self._send(200, "text/html; charset=utf-8", page.encode())
        if u.path == "/api/state":
            return self._send(200, "application/json", json.dumps(SH.snapshot()).encode())
        if u.path == "/api/frame.jpg":
            with SH.lock:
                f = SH.frame_jpeg
            if not f:
                return self._send(503, "text/plain", b"no frame yet")
            return self._send(200, "image/jpeg", f)
        if u.path == "/api/control":
            cmd = (q.get("cmd") or ["run"])[0]
            if cmd not in ("run", "pause", "stop"):
                return self._send(400, "text/plain", b"bad cmd")
            os.makedirs(os.path.dirname(CONTROL), exist_ok=True)
            with open(CONTROL, "w") as fh:
                fh.write(cmd)
            SH.note(f"control -> {cmd}")
            return self._send(200, "application/json", json.dumps({"ok": cmd}).encode())
        if u.path == "/api/tasks":
            name = (q.get("set") or [""])[0]
            if name:
                on = (q.get("on") or ["1"])[0] == "1"
                with TS.lock:
                    if name in TS.tasks:
                        TS.tasks[name] = on
                TS.save()
                SH.note(f"task {name} -> {'on' if on else 'off'}")
            return self._send(200, "application/json",
                              json.dumps(TS.snapshot()).encode())
        if u.path == "/api/option":
            k = (q.get("key") or [""])[0]
            v = (q.get("value") or [""])[0]
            if k:
                with TS.lock:
                    if k in TS.options:
                        cur = TS.options[k]
                        try:
                            TS.options[k] = type(cur)(v) if not isinstance(cur, bool) else v == "1"
                        except (TypeError, ValueError):
                            return self._send(400, "text/plain",
                                              f"bad value for {k}".encode())
                TS.save()
                SH.note(f"option {k} = {TS.options.get(k)}")
            return self._send(200, "application/json",
                              json.dumps(TS.snapshot()).encode())
        if u.path == "/api/focus":
            r = focus_emulator()
            SH.note("focus game -> " + json.dumps(r))
            return self._send(200, "application/json", json.dumps(r).encode())
        if u.path == "/api/arm":
            # Toggle which states live mode may act on, without a restart.
            if RUNNER is None:
                return self._send(200, "application/json",
                                  json.dumps({"ok": False, "error": "no runner"}).encode())
            st = (q.get("states") or [""])[0]
            want = {x.strip() for x in st.split(",") if x.strip()}
            RUNNER.allow = want
            RUNNER.live = bool(want)
            SH.mode = ("LIVE:" + ",".join(sorted(want))) if want else "dry-run"
            SH.note(f"armed states -> {sorted(want) or 'none (dry-run)'}")
            return self._send(200, "application/json",
                              json.dumps({"ok": True, "armed": sorted(want)}).encode())
        if u.path == "/api/config":
            p = os.path.join(ROOT, "configs/daily_reward.json")
            return self._send(200, "application/json", open(p, "rb").read())
        return self._send(404, "text/plain", b"not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--no-bot", action="store_true", help="serve UI only")
    ap.add_argument("--no-pin", action="store_true")
    ap.add_argument("--embed", action="store_true",
                    help="put the game in an IFRAME inside our page. MEASURED "
                         "BROKEN for this site: the same URL loaded top-level is "
                         "logged in, but inside a 127.0.0.1 iframe it shows the "
                         "logged-out landing page, because a SameSite=Lax session "
                         "cookie is never sent cross-site. The default mode paints "
                         "CDP frames instead and gives the same single-window view.")
    ap.add_argument("--no-stream", action="store_true",
                    help="do not encode frames for the web view. Worth using now "
                         "that the in-page panel gives a native, lag-free view in "
                         "the game tab itself; streaming is only needed if you "
                         "want the picture inside the localhost dashboard too.")
    ap.add_argument("--stream", action="store_true",
                    help="deprecated: streaming is on by default; use --no-stream "
                         "to turn it off")
    ap.add_argument("--no-overlay", action="store_true",
                    help="do not draw the status panel into the game page. "
                         "ON by default now that the viewport is 1720x720: the "
                         "game canvas occupies CSS x 380..1340, so the panel sits "
                         "in the free page margin and occludes NO game pixels. "
                         "MEASURED: panel at CSS x 1401..1701, canvas ends at "
                         "1340. This gives native rendering with a panel beside "
                         "it - no JPEG streaming, no lag. At a 960-wide viewport "
                         "there is no margin and the panel WOULD cover the "
                         "gold/token HUD, so turn it off if you narrow the "
                         "viewport.")
    ap.add_argument("--overlay", action="store_true",
                    help="deprecated: the overlay is already on by default")
    ap.add_argument("--live", action="store_true",
                    help="permit clicking, but ONLY for states named in --allow")
    ap.add_argument("--allow", default="",
                    help="comma-separated states live mode may act on, e.g. character_select")
    ap.add_argument("--sweep", action="store_true",
                    help="full 36-step scale sweep: ~16s/cycle, calibration only")
    ap.add_argument("--config", default=os.path.join(ROOT, "configs/daily_reward.json"))
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    global GAME_URL
    GAME_URL = cfg.get("target", {}).get("game_url", "")
    if not GAME_URL:
        print("  config has no target.game_url"); return 2
    os.makedirs(os.path.join(ROOT, "run"), exist_ok=True)
    if os.path.exists(CONTROL):
        os.remove(CONTROL)                     # never start up paused/stopped

    if not args.no_bot:
        allow = [x.strip() for x in args.allow.split(",") if x.strip()]
        SH.mode = f"LIVE:{','.join(allow)}" if (args.live and allow) else "dry-run"
        if args.live and not allow:
            print("  --live given with no --allow: nothing is armed (dry everywhere)")
        global RUNNER
        RUNNER = Runner(cfg, args.cdp_port, not args.no_pin, args.interval,
               SCALES if args.sweep else SCALES_FAST,
               live=args.live, allow=allow,
               stream=not args.no_stream, use_overlay=not args.no_overlay,
               embed=args.embed, dash_port=args.port)
        RUNNER.start()
    else:
        SH.status = "UI only (--no-bot)"
        SH.note("UI-only mode: no CDP, no frames")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"\n  dashboard -> http://127.0.0.1:{args.port}\n"
          f"  bound to localhost only.  mode: {SH.mode}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
