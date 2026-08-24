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
SCALES_FAST = [1.00]   # geometry pinned -> templates match at 1.0 exactly


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
        cap = Capture(c)
        actor = Actor(c, cap, _Log(), dry_run=not self.live)
        SH.note(f"capture viewport={cap.viewport} dpr={cap.dpr}")

        class _L:                          # load_templates wants a logger
            info = staticmethod(lambda *a: SH.note(a[0] % a[1:] if len(a) > 1 else a[0]))
            warning = info
        tpls = load_templates(self.cfg, _L)

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
            page = EMBED.replace("__GAME_URL__", GAME_URL)
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
                    help="render the game INSIDE the dashboard page and drive it "
                         "there (native rendering, no streaming, no lag)")
    ap.add_argument("--stream", action="store_true",
                    help="also encode frames for the web view (adds lag; the "
                         "in-page overlay is the default and has none)")
    ap.add_argument("--no-overlay", action="store_true")
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
               stream=args.stream, use_overlay=not args.no_overlay,
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
