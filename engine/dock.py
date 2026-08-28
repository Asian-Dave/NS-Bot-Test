#!/usr/bin/env python3
"""The control dock — the bot's UI, injected into the game page itself.

WHY THE UI LIVES INSIDE THE GAME PAGE
-------------------------------------
The dashboard streamed captured frames to a page on 127.0.0.1. That works, but
it is laggy and clunky to watch, because the view is only as fast as our
capture -> encode -> poll cycle, and it is a second window besides.

The alternative people reach for is "a native app window". Worth noticing what
the reference bot actually IS before copying it: Adobe AIR + CEF, i.e. a
**Chromium embedded in a native frame**. Native shell and browser were never
alternatives for them either - they just hid the browser chrome.

We can have the same thing for almost nothing, because we already own the
browser and the control channel:

    * `browser.launch(..., app_mode=True)` starts Chrome with `--app=<url>`,
      which has no tabs, no omnibox and no bookmarks bar - a plain window.
    * this module injects the control panel INTO that page.

The operator then watches the REAL canvas at native framerate. There is no
capture in the viewing path at all.

WHERE IT SITS, AND WHY THAT EXACT PLACE
---------------------------------------
CLAUDE.md has a hard rule: **never resize the `ruffle-player` element via CSS.**
Doing so desyncs click -> stage coordinate mapping inside the SWF and looks
exactly like a hang. So the dock must not lay the game out differently.

It does not have to. Measured on the live page at the pinned 1720x720 viewport:

    game iframe (emulator.html)   x=375  w=960   -> right edge 1335
    viewport                                          width 1720
    free right gutter             1335..1720     ->  385 CSS px

That gutter is page wallpaper. A `position:fixed` panel of 380 px sits in it as
a SIBLING of the game: nothing about the player element changes, and the live
check `overlaps: false` is asserted at install time rather than assumed.

HOW A BUTTON REACHES THE BOT
----------------------------
`Runtime.addBinding` exposes `window.__nsbot_send(str)` in the page. A click
calls it and the string arrives in Python as a `Runtime.bindingCalled` event on
the CDP socket we already hold. No HTTP server, no port, no polling the DOM.

This needed a fix in `cdp.py` first: `call()` used to DISCARD every message that
was not its own reply, so events did not exist as far as this codebase was
concerned - silently, with nothing erroring. See `CDP._stash` / `drain_events`.

THE PANEL IS A CLICK EXCLUSION ZONE
-----------------------------------
The bot clicks by CDP at page coordinates. Every measured target is inside the
game rect, so the dock should never be a target - but "should never" is how the
card solver ended up clicking into the weapon Shop. `dock_rect()` is published
so `Actor` can REFUSE any bot click that lands in it, the same way the token `+`
buttons are handled.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BINDING = "__nsbot_send"
PANEL_ID = "__nsbot_dock"
WIDTH = 380                       # CSS px; the measured gutter is 385

_CSS = """
#__ID__{position:fixed;top:0;right:0;width:__W__px;height:100vh;z-index:2147483647;
  box-sizing:border-box;padding:10px 12px 14px;overflow:auto;
  font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:#e6e9ef;
  background:#171a21;border-left:1px solid #2b313d;pointer-events:auto;
  -webkit-font-smoothing:antialiased}
#__ID__ *{box-sizing:border-box}
#__ID__ h4{margin:12px 0 6px;font-size:10px;letter-spacing:.11em;
  text-transform:uppercase;color:#8b94a7;font-weight:600}
#__ID__ h4:first-child{margin-top:0}
#__ID__ .hd{display:flex;justify-content:space-between;align-items:center;
  margin-bottom:10px}
#__ID__ .hd b{font-size:12px;letter-spacing:.08em}
#__ID__ .pill{font-size:10px;padding:2px 8px;border-radius:9px;
  border:1px solid #39414f;color:#8b94a7}
#__ID__ .pill.run{color:#4ade80;border-color:#2f6b45}
#__ID__ .pill.pause{color:#fbbf24;border-color:#6b5620}
#__ID__ .pill.stop{color:#f87171;border-color:#6b3030}
#__ID__ .row{display:flex;justify-content:space-between;gap:10px;padding:2px 0}
#__ID__ .d{color:#8b94a7}
#__ID__ .ok{color:#4ade80}
#__ID__ .warn{color:#fbbf24}
#__ID__ .bad{color:#f87171}
#__ID__ .g{display:grid;grid-template-columns:1fr 1fr;gap:6px}
#__ID__ button{font:inherit;padding:7px 4px;cursor:pointer;background:#232833;
  color:#e6e9ef;border:1px solid #39414f;border-radius:5px;transition:none}
#__ID__ button:hover{background:#2b313d}
#__ID__ button:active{background:#39414f}
#__ID__ button[disabled]{opacity:.4;cursor:default}
#__ID__ button.on{border-color:#4ade80;color:#4ade80}
#__ID__ button.danger:hover{border-color:#6b3030;color:#f87171}
#__ID__ hr{border:0;border-top:1px solid #2b313d;margin:10px 0}
#__ID__ .log{font-size:11px;color:#8b94a7;white-space:pre-wrap;word-break:break-word;
  max-height:26vh;overflow:auto}
#__ID__ .log b{color:#c9d1e0;font-weight:400}
"""

# Runs on EVERY document (addScriptToEvaluateOnNewDocument), so it must be
# idempotent and must not assume the DOM exists yet.
_BOOTSTRAP = r"""
(() => {
  if (window.top !== window) return "iframe-skip";   // never inside the game
  if (window.__nsbotDockInit) return "already";
  window.__nsbotDockInit = true;

  const ID = "__ID__", CSS = __CSS__;

  const send = (cmd, arg) => {
    try { window.__BINDING__(JSON.stringify({cmd, arg, t: Date.now()})); }
    catch (e) { /* binding not attached yet; the click is simply dropped */ }
  };
  window.__nsbotSend = send;

  const build = () => {
    if (document.getElementById(ID)) return;
    const st = document.createElement("style");
    st.textContent = CSS;
    document.documentElement.appendChild(st);
    const d = document.createElement("div");
    d.id = ID;
    d.addEventListener("click", ev => {
      const b = ev.target.closest("button[data-cmd]");
      if (!b) return;
      ev.preventDefault(); ev.stopPropagation();
      send(b.dataset.cmd, b.dataset.arg || null);
    });
    document.documentElement.appendChild(d);
    window.__nsbotRender(window.__nsbotState || {});
  };

  // ---- FOCUS MODE -------------------------------------------------------
  // Hides everything on the page except the game, and pins it to the top of
  // the viewport.
  //
  // This is not only cosmetic. The page is much taller than the viewport, so
  // where the game sits depends on the scroll position, and that DRIFTED across
  // a session (scrollY 458 -> 301 -> 242). Every drift moved the game with it:
  // template anchors fell into the hidden band and the bot reported "could not
  // find the Special tab" and "no anchor matched" on screens that were
  // perfectly healthy, just scrolled away.
  //
  // Hiding the siblings lets the game reflow to the top, so scrollY is 0 and
  // stays 0. Deterministic geometry is the real prize; the calm is a bonus.
  //
  // It hides SIBLINGS ONLY and never touches the game element itself.
  // CLAUDE.md is emphatic that resizing `ruffle-player` via CSS desyncs
  // click -> stage mapping inside the SWF and looks exactly like a hang.
  const gameEl = () =>
    document.querySelector('iframe[src*="emulator"]') ||
    document.querySelector('iframe[src*="play"]');

  window.__nsbotFocus = (on) => {
    const g = gameEl();
    if (!g) return "no-game";
    // IDEMPOTENT. Re-applying focus recomputed the margin nudge from whatever
    // the layout happened to be at that instant, so calling it every cycle made
    // the game JUMP - and every jump moved the anchors, which the state
    // classifier then read as "unknown". Focus is a one-shot: if it is already
    // on, say so and change nothing.
    if (on && window.__nsbotFocusOn) return "already";
    if (!on && !window.__nsbotFocusOn) return "already";
    if (on) {
      for (let el = g; el && el !== document.documentElement; el = el.parentElement) {
        for (const sib of Array.from(el.parentElement ? el.parentElement.children : [])) {
          if (sib === el || sib.id === ID || sib.tagName === "STYLE") continue;
          if (!sib.hasAttribute("data-nsbot-hid")) {
            sib.setAttribute("data-nsbot-hid", sib.style.display || "");
            sib.style.display = "none";
          }
        }
      }
      document.body.style.margin = "0";
      // TOP-ALIGN, do not centre. The game is 839 CSS px tall in a 720 px
      // viewport, so 119 px is cut off whatever we do - but WHICH 119 matters.
      // Left to itself the layout centres the game and loses 59 px off the top,
      // which is exactly where the panel tabs and headers live. Aligning the
      // top instead puts the whole upper game in view and sacrifices the NPC
      // rail at the bottom, which nothing here needs.
      // Nudge with a MARGIN, never a size. Once the siblings are hidden the
      // document is no taller than the game, so there is nothing left to
      // scroll - the remaining offset is the container centring the game, and
      // scrollTo cannot undo it. A margin shifts the iframe without touching
      // its width or height, so the SWF's own scaling is unaffected. Resizing
      // it would desync click -> stage mapping inside the game (CLAUDE.md).
      scrollTo(0, 0);
      const r0 = g.getBoundingClientRect();
      if (Math.abs(r0.y) > 1) {
        if (!g.hasAttribute("data-nsbot-mt")) {
          g.setAttribute("data-nsbot-mt", g.style.marginTop || "");
        }
        const cur = parseFloat(getComputedStyle(g).marginTop) || 0;
        g.style.marginTop = Math.round(cur - r0.y) + "px";
      }
    } else {
      document.querySelectorAll("[data-nsbot-hid]").forEach(el => {
        el.style.display = el.getAttribute("data-nsbot-hid");
        el.removeAttribute("data-nsbot-hid");
      });
      if (g.hasAttribute("data-nsbot-mt")) {
        g.style.marginTop = g.getAttribute("data-nsbot-mt");
        g.removeAttribute("data-nsbot-mt");
      }
    }
    window.__nsbotFocusOn = !!on;
    return on ? "focused" : "restored";
  };

  // Re-run ONLY the top-alignment. Focus is applied as soon as the game appears,
  // which can be before the layout has settled - measured, the nudge computed a
  // correction of ~0 and the game then settled 58 px high, cutting the top off
  // the panel. This converges: once the game is at y=0 it changes nothing, so it
  // is safe to call again.
  window.__nsbotAlign = () => {
    const g = gameEl();
    if (!g || !window.__nsbotFocusOn) return "not-focused";
    const r = g.getBoundingClientRect();
    if (Math.abs(r.y) <= 1) return "aligned";
    if (!g.hasAttribute("data-nsbot-mt")) {
      g.setAttribute("data-nsbot-mt", g.style.marginTop || "");
    }
    const cur = parseFloat(getComputedStyle(g).marginTop) || 0;
    g.style.marginTop = Math.round(cur - r.y) + "px";
    return "realigned";
  };

  // Renders by UPDATING values, never by rewriting the panel.
  //
  // The first version replaced innerHTML on every cycle. Since `cycle` and
  // `uptime` change every second that meant a full repaint every second: every
  // button was destroyed and recreated, so the focus toggle and the active-task
  // highlight visibly flashed on and off, hover states dropped, and the whole
  // panel looked loose and unstable. Nothing was actually toggling - focus was
  // applied exactly once - but you cannot tell that by looking at it.
  //
  // So the skeleton is built ONCE and only text and classes are touched after.
  const V = {};                       // id -> element, for the value cells

  const skeleton = (el, s) => {
    const row = (k) =>
      `<div class="row"><span class="d">${k}</span><span id="v_${k}"></span></div>`;
    const btn = (cmd, label, arg) =>
      `<button data-cmd="${cmd}"${arg ? ` data-arg="${arg}"` : ""}>${label}</button>`;
    el.innerHTML =
      `<div class="hd"><b>NS BOT</b><span class="pill" id="v_pill"></span></div>` +
      row("state") + row("task") + row("cycle") + row("uptime") +
      `<div class="d" id="v_note" style="margin-top:8px"></div>` +
      `<h4>Task</h4><div class="g" id="v_tasks"></div>` +
      `<h4>Run</h4><div class="g">` +
        btn("run", "Run") + btn("pause", "Pause") +
        btn("relog", "Relog") + btn("stop", "Stop") +
      `</div>` +
      `<h4>View</h4><div style="margin-top:2px">` +
        btn("focus", "Focus mode") +
      `</div>` +
      `<div style="margin-top:6px">` +
        btn("quit", "Quit (closes this panel)") +
      `</div>` +
      `<h4>Log</h4><div class="log" id="v_log"></div>`;
    el.querySelectorAll("[id^=v_]").forEach(n => { V[n.id] = n; });
    el.querySelector('[data-cmd="stop"]').classList.add("danger");
    el.querySelector('[data-cmd="quit"]').classList.add("danger");
    (s.tasks || []).forEach(t => {
      const b = document.createElement("button");
      b.dataset.cmd = "task"; b.dataset.arg = t.key; b.textContent = t.label;
      V.v_tasks.appendChild(b);
    });
  };

  const setText = (id, txt, cls) => {
    const n = V[id];
    if (!n) return;
    const t = String(txt);
    if (n.textContent !== t) n.textContent = t;
    if (cls !== undefined && n.className !== cls) n.className = cls;
  };
  const setOn = (btn, on) => {
    if (!btn) return;
    if (btn.classList.contains("on") !== !!on) btn.classList.toggle("on", !!on);
  };

  window.__nsbotRender = (s) => {
    const el = document.getElementById(ID);
    if (!el) return "missing";
    window.__nsbotState = s;
    if (!V.v_state) skeleton(el, s);

    const mode = s.mode || "idle";
    setText("v_pill", mode,
            "pill " + (mode === "running" ? "run"
                     : mode === "paused" ? "pause"
                     : mode === "stopped" ? "stop" : ""));
    setText("v_state", s.state || "-", s.state === "unknown" ? "warn" : "ok");
    setText("v_task", s.task || "-");
    setText("v_cycle", s.cycle == null ? "-" : s.cycle);
    setText("v_uptime", s.uptime || "-");
    setText("v_note", s.note || "");

    el.querySelectorAll('[data-cmd="task"]').forEach(b =>
      setOn(b, b.dataset.arg === s.task));
    setOn(el.querySelector('[data-cmd="run"]'), mode === "running");
    setOn(el.querySelector('[data-cmd="pause"]'), mode === "paused");
    setOn(el.querySelector('[data-cmd="stop"]'), mode === "stopped");
    const fb = el.querySelector('[data-cmd="focus"]');
    if (fb) {
      const want = s.focus ? "Focus mode: ON" : "Focus mode: off";
      if (fb.textContent !== want) fb.textContent = want;
      setOn(fb, !!s.focus);
    }
    const lg = (s.log || []).join("\n");
    if (V.v_log && V.v_log.textContent !== lg) V.v_log.textContent = lg;
    return "ok";
  };

  if (document.documentElement) build();
  else document.addEventListener("DOMContentLoaded", build, {once: true});
  return "installed";
})()
"""


class Dock:
    """Install, update and read the in-page control panel."""

    def __init__(self, cdp, log=None, width=WIDTH):
        self.cdp, self.log, self.width = cdp, log, width
        self._script_id = None

    # -- install -----------------------------------------------------------
    def _source(self):
        css = _CSS.replace("__ID__", PANEL_ID).replace("__W__", str(self.width))
        return (_BOOTSTRAP
                .replace("__ID__", PANEL_ID)
                .replace("__BINDING__", BINDING)
                .replace("__CSS__", json.dumps(css)))

    def install(self, verify=True):
        """Attach the binding, inject now, and re-inject on every navigation.

        Re-injection matters: a relog or any reload throws the panel away, and a
        control panel that disappears the first time the session bounces is not a
        control panel.
        """
        self.cdp.call("Page.enable")
        self.cdp.watch("Runtime.bindingCalled")
        self.cdp.add_binding(BINDING)
        # Clear any PREVIOUS injection first. The bootstrap guards on
        # `__nsbotDockInit` so it is idempotent across navigations, but that same
        # guard makes it ignore a NEWER version of itself - so after the module
        # changed, install() silently kept running the old code and new controls
        # came back "no-panel".
        try:
            self.cdp.evaluate(
                f"(()=>{{document.getElementById({PANEL_ID!r})?.remove();"
                f"window.__nsbotDockInit=false;return 1;}})()")
        except Exception:
            pass
        src = self._source()
        r = self.cdp.call("Page.addScriptToEvaluateOnNewDocument", source=src)
        self._script_id = r.get("identifier")
        self.cdp.evaluate(src)                       # and into the live document
        info = self.geometry()
        if verify and info.get("overlaps"):
            # Refuse rather than lay the game out differently - CLAUDE.md's
            # hard rule is that the player element must never be resized.
            self.remove()
            raise RuntimeError(
                f"dock would overlap the game ({info}); refusing to install "
                "rather than reflow the player element")
        if self.log:
            self.log.info("dock installed: %s", info)
        return info

    def remove(self):
        if self._script_id:
            try:
                self.cdp.call("Page.removeScriptToEvaluateOnNewDocument",
                              identifier=self._script_id)
            except Exception:
                pass
            self._script_id = None
        self.cdp.evaluate(
            f"(()=>{{document.getElementById({PANEL_ID!r})?.remove();"
            f"window.__nsbotDockInit=false;return 'removed';}})()")

    # -- geometry ----------------------------------------------------------
    def geometry(self):
        """Where the game and the dock are, in CSS px, plus the overlap check."""
        js = """JSON.stringify((()=>{
          const f=document.querySelector('iframe[src*="emulator"]')
                 ||document.querySelector('iframe[src*="play"]');
          const r=f?f.getBoundingClientRect():null;
          const el=document.getElementById(%s);
          const d=el?el.getBoundingClientRect():null;
          return {viewport:[innerWidth,innerHeight], dpr:devicePixelRatio,
                  game: r?[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]:null,
                  dock: d?[Math.round(d.x),Math.round(d.y),Math.round(d.width),Math.round(d.height)]:null,
                  overlaps: !!(r&&d) && (r.x+r.width) > d.x};
        })())""" % repr(PANEL_ID)
        try:
            return json.loads(self.cdp.evaluate(js) or "{}")
        except Exception:
            return {}

    def dock_rect(self, dpr=None):
        """The panel in CAPTURED pixels — the no-click zone for `Actor`."""
        g = self.geometry()
        d = g.get("dock")
        if not d:
            return None
        s = dpr if dpr is not None else g.get("dpr", 1) or 1
        x, y, w, h = d
        return (int(x * s), int(y * s), int(w * s), int(h * s))

    # -- state out, commands in -------------------------------------------
    def render(self, state):
        payload = json.dumps(state)
        return self.cdp.evaluate(
            f"(window.__nsbotRender ? window.__nsbotRender({payload}) : 'no-panel')")

    def focus(self, on=True):
        """Hide the rest of the page so only the game (and this panel) shows."""
        return self.cdp.evaluate(
            f"(window.__nsbotFocus ? window.__nsbotFocus({str(bool(on)).lower()})"
            f" : 'no-panel')")

    def align(self):
        """Re-assert top alignment once the layout has settled."""
        return self.cdp.evaluate(
            "(window.__nsbotAlign ? window.__nsbotAlign() : 'no-panel')")

    def focus_state(self):
        try:
            return bool(self.cdp.evaluate("!!window.__nsbotFocusOn"))
        except Exception:
            return False

    def game_ready(self):
        """Is the game actually loaded — i.e. is the SWF's iframe present?"""
        try:
            return bool(self.cdp.evaluate(
                '!!(document.querySelector(\'iframe[src*="emulator"]\')'
                ' || document.querySelector(\'iframe[src*="play"]\'))'))
        except Exception:
            return False

    def commands(self, poll=0.0):
        """Buttons the operator pressed since the last call, oldest first."""
        out = []
        for ev in self.cdp.drain_events("Runtime.bindingCalled", poll=poll):
            p = ev.get("params", {})
            if p.get("name") != BINDING:
                continue
            try:
                out.append(json.loads(p.get("payload") or "{}"))
            except ValueError:
                continue
        return out


def main():
    import argparse
    import time
    from capture import Capture
    from cdp import CDP, find_page_target

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--watch", type=float, default=0.0,
                    help="stay attached this many seconds, printing button presses")
    a = ap.parse_args()

    class _Log:
        def info(self, m, *x):
            print(("  " + m) % x if x else "  " + m, flush=True)
        warning = error = info

    t = find_page_target(port=a.port, url_contains="ninjasaga", timeout=20)
    c = CDP(t["webSocketDebuggerUrl"])
    c.call("Page.enable")
    d = Dock(c, _Log())
    try:
        if a.remove:
            d.remove()
            print("dock removed")
            return 0
        print("geometry:", d.install())
        print("dock rect (captured px):", d.dock_rect())
        d.render({"mode": "idle", "state": "lobby", "task": "tp_scroll",
                  "cycle": 0, "uptime": "0s",
                  "tasks": [{"key": "farm_missions", "label": "Farm missions"},
                            {"key": "tp_training", "label": "TP training"},
                            {"key": "resume_to_lobby", "label": "Resume to lobby"},
                            {"key": "daily_reward", "label": "Daily reward"}],
                  "log": ["dock installed"]})
        end = time.time() + a.watch
        while time.time() < end:
            for cmd in d.commands(poll=0.4):
                print("  BUTTON", cmd)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
