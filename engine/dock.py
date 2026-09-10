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
/* READY is "attached, nothing running" - deliberately not amber, because
   it is not a warning: a one-shot task finished and the supervisor is
   still holding the session. */
#__ID__ .pill.ready{color:#7dd3fc;border-color:#2b5b73}
#__ID__ .row{display:flex;justify-content:space-between;gap:10px;padding:2px 0}
#__ID__ .d{color:#8b94a7}
#__ID__ .ok{color:#4ade80}
#__ID__ .warn{color:#fbbf24}
#__ID__ .bad{color:#f87171}
#__ID__ .g{display:grid;grid-template-columns:1fr 1fr;gap:6px}
#__ID__ .g4{display:grid;grid-template-columns:repeat(4,1fr);gap:4px}
#__ID__ .g5{display:grid;grid-template-columns:repeat(5,1fr);gap:4px}
#__ID__ .g5 button{padding:5px 0;font-size:10px}
#__ID__ .g4 button{padding:5px 0;font-size:11px}
#__ID__ button{font:inherit;padding:7px 4px;cursor:pointer;background:#232833;
  color:#e6e9ef;border:1px solid #39414f;border-radius:5px;transition:none}
#__ID__ button:hover{background:#2b313d}
#__ID__ button:active{background:#39414f}
#__ID__ button[disabled]{opacity:.4;cursor:default}
#__ID__ button.on{border-color:#4ade80;color:#4ade80}
#__ID__ button.danger:hover{border-color:#6b3030;color:#f87171}
#__ID__ hr{border:0;border-top:1px solid #2b313d;margin:10px 0}
#__ID__.stale{opacity:.72}
#__ID__ .stale-note{background:#3a2a12;border:1px solid #6b5620;color:#fbbf24;
  border-radius:6px;padding:7px 8px;margin-bottom:9px;font-size:11px;line-height:1.5}
#__ID__ .stale-note code{color:#e6e9ef;font-size:10px;word-break:break-all}
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
      // PIN THE SITE'S OWN POSITIONING INSTEAD OF FIGHTING IT.
      //
      // The endless re-aligning had one cause: the SITE's JavaScript sets
      //     #game-container { position:absolute; top:-58.5px }
      // pushing the container up by the overflow (the game is 839 CSS tall in a
      // 780 wrapper). Focus mode counteracted that with a fixed marginTop on
      // the iframe - but the site RECOMPUTES that top on any reflow, so a fixed
      // number only cancels it at one particular layout, and every reflow put
      // the game back out of place. That is why it needed re-aligning forever.
      //
      // An !important STYLESHEET declaration beats an inline non-important one,
      // so this cannot be undone by the site rewriting its inline style.
      // Verified: pinned to y=0, and still 0 after re-setting top=-58.5px
      // inline. Position only - never a size, which would desync click -> stage
      // mapping inside the SWF.
      let pin = document.getElementById("__nsbot_pin");
      if (!pin) {
        pin = document.createElement("style");
        pin.id = "__nsbot_pin";
        document.documentElement.appendChild(pin);
      }
      pin.textContent = "#game-container{top:0 !important;}";
      const r0 = g.getBoundingClientRect();
      if (Math.abs(r0.y) > 1) {
        if (!g.hasAttribute("data-nsbot-mt")) {
          g.setAttribute("data-nsbot-mt", g.style.marginTop || "");
        }
        const cur = parseFloat(getComputedStyle(g).marginTop) || 0;
        g.style.marginTop = Math.round(cur - r0.y) + "px";
      }
      // NOT LEFT-ALIGNED, and that was tried and REVERTED. Nudging marginLeft
      // as well looked right in a single measurement and broke in the loop:
      // both corrections were computed from ONE rect, but changing marginLeft
      // REFLOWS the page and invalidates the r.y used a moment later. Live, the
      // margins ran away - marginTop reached 177 px - and the game ended up
      // shifted with its top clipped. Fixing it needs a re-measure between the
      // two corrections and a convergence check per axis; until that is done
      // and verified, one axis only.
    } else {
      document.querySelectorAll("[data-nsbot-hid]").forEach(el => {
        el.style.display = el.getAttribute("data-nsbot-hid");
        el.removeAttribute("data-nsbot-hid");
      });
      if (g.hasAttribute("data-nsbot-mt")) {
        g.style.marginTop = g.getAttribute("data-nsbot-mt");
        g.removeAttribute("data-nsbot-mt");
      }
      const pin0 = document.getElementById("__nsbot_pin");
      if (pin0) pin0.remove();
      if (g.hasAttribute("data-nsbot-ml")) {
        g.style.marginLeft = g.getAttribute("data-nsbot-ml");
        g.removeAttribute("data-nsbot-ml");
      }
    }
    window.__nsbotFocusOn = !!on;
    // THE LOCK RIDES WITH FOCUS MODE, and that is deliberate rather than
    // convenient. With focus ON the game is pinned at scrollY 0 and any scroll
    // is an accident. With focus OFF the bot SCROLLS THE GAME ON PURPOSE -
    // `Capture.scroll_game(frac)` is the documented fallback for reaching an
    // anchor in the hidden 119 px band, and the resume ladder alternates the
    // scroll before calling a frame unrecognised. Locking there would break
    // the one path that needs scrolling.
    if (window.__nsbotScrollLock) window.__nsbotScrollLock(!!on);
    return on ? "focused" : "restored";
  };

  // ---------------------------------------------------------------------
  // SCROLL LOCK — an accidental wheel must not move the game under the bot
  //
  // The operator reported "sometimes I accidentally scroll down which causes
  // some kind of behaviour bug". It does, and this file already documents why:
  // every minigame's geometry is ABSOLUTE, so a displaced game breaks all of
  // them at once, each reporting a fault in its own subsystem and none naming
  // the real cause. Measured previously: scrollY 60 put the game at -236
  // CAPTURED px and the memory board's rows measured -237 out.
  //
  // `__nsbotAlign` already puts the scroll back - but it is only called from
  // `ensure_focus`, which runs BETWEEN cycles, and a mission blocks for
  // minutes. So a scroll during a mission stood for the whole of it. Snapping
  // back has to be event-driven and live IN THE PAGE, for exactly the reason
  // the panel's heartbeat does: Python is not there to notice.
  //
  // Three layers, because each catches what the others miss:
  //   1. overflow:hidden via an !important STYLESHEET - removes the scroll
  //      rather than reacting to it, and beats the site's inline styles.
  //   2. a scroll listener that snaps to 0 - catches anything that still
  //      scrolls (keyboard, the site's own script, a focus() on some element).
  //      `scroll` cannot be prevented, only undone.
  //   3. wheel/touchmove preventDefault - stops the gesture before it scrolls,
  //      which avoids a visible jump-and-snap.
  //
  // THE PANEL'S OWN LOG PANE MUST STAY SCROLLABLE. It is `overflow:auto` and
  // the operator reads it, so layer 3 exempts any event inside the dock. Layer
  // 1 cannot affect it: the dock is position:fixed with its own scroller.
  //
  // KEYS ARE DELIBERATELY NOT BLOCKED. This is a Flash game and the SWF may
  // want them; a swallowed keystroke would be a new bug to chase. Layer 2
  // covers a keyboard scroll after the fact.
  //
  // Snaps are COUNTED, not silent. "the game drifted" was invisible before,
  // and a counter turns an accidental scroll into something the log can name.
  window.__nsbotScrollSnaps = window.__nsbotScrollSnaps || 0;
  window.__nsbotScrollLock = (on) => {
    const ID = "__nsbot_scrolllock";
    if (on) {
      if (!window.__nsbotScrollHandlers) {
        const snap = () => {
          if (window.scrollY !== 0 || window.scrollX !== 0) {
            window.__nsbotScrollSnaps++;
            window.scrollTo(0, 0);
          }
        };
        const inDock = (ev) => {
          const d = document.getElementById("__ID__");
          return d && ev.target && d.contains(ev.target);
        };
        const block = (ev) => {
          if (inDock(ev)) return;          // the log pane scrolls; leave it
          ev.preventDefault();
          snap();
        };
        window.addEventListener("scroll", snap, {passive: true});
        window.addEventListener("wheel", block, {passive: false});
        window.addEventListener("touchmove", block, {passive: false});
        window.__nsbotScrollHandlers = {snap, block};
      }
      let st = document.getElementById(ID);
      if (!st) {
        st = document.createElement("style");
        st.id = ID;
        document.documentElement.appendChild(st);
      }
      st.textContent =
        "html,body{overflow:hidden !important;overscroll-behavior:none !important;}";
      window.scrollTo(0, 0);
      window.__nsbotScrollLocked = true;
      return "locked";
    }
    const h = window.__nsbotScrollHandlers;
    if (h) {
      window.removeEventListener("scroll", h.snap, {passive: true});
      window.removeEventListener("wheel", h.block, {passive: false});
      window.removeEventListener("touchmove", h.block, {passive: false});
      window.__nsbotScrollHandlers = null;
    }
    const st0 = document.getElementById(ID);
    if (st0) st0.remove();
    window.__nsbotScrollLocked = false;
    return "unlocked";
  };

  // Re-run ONLY the top-alignment. Focus is applied as soon as the game appears,
  // which can be before the layout has settled - measured, the nudge computed a
  // correction of ~0 and the game then settled 58 px high, cutting the top off
  // the panel. This converges: once the game is at y=0 it changes nothing, so it
  // is safe to call again.
  window.__nsbotAlign = () => {
    const g = gameEl();
    if (!g || !window.__nsbotFocusOn) return "not-focused";
    // The page can scroll out from under us even in focus mode; rect.y is
    // viewport-relative, so put the scroll back first and then measure.
    if (window.scrollY !== 0 || window.scrollX !== 0) window.scrollTo(0, 0);
    // Re-assert the SCROLL LOCK as well. A reload drops the injected <style>
    // and the listeners with it, and this project's standing rule is that any
    // cached belief about page state is invalidated by a navigation - so ask
    // the page rather than remembering. Idempotent, so calling it every cycle
    // costs nothing.
    if (!window.__nsbotScrollLocked && window.__nsbotScrollLock) {
      window.__nsbotScrollLock(true);
    }
    // Re-assert the pin: a reload drops the injected <style>, and without it the
    // site's own top:-58.5px takes over again.
    if (!document.getElementById("__nsbot_pin")) {
      const st = document.createElement("style");
      st.id = "__nsbot_pin";
      st.textContent = "#game-container{top:0 !important;}";
      document.documentElement.appendChild(st);
    }
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
      `<div id="v_stale" class="stale-note" style="display:none">` +
        `no bot attached — the panel is frozen.<br>run:<br>` +
        `<code>.venv/bin/python engine/app.py --attach</code></div>` +
      row("state") + row("task") + row("cycle") + row("uptime") +
      `<div class="d" id="v_note" style="margin-top:8px"></div>` +
      `<h4>Task</h4><div class="g" id="v_tasks"></div>` +
      `<h4>Run</h4><div class="g">` +
        btn("run", "Run") + btn("pause", "Pause") +
        btn("relog", "Relog") + btn("stop", "Stop (kills + resets)") +
      `</div>` +
      `<h4>Farm target</h4>` +
      `<div class="row"><span class="d">grade</span><span id="v_grade"></span></div>` +
      `<div class="g5" id="v_grades"></div>` +
      `<div class="row" style="margin-top:5px"><span class="d">mission</span>` +
        `<span id="v_pin"></span></div>` +
      `<div class="g5" style="margin-top:3px">` +
        btn("pin_off", "Highest") + btn("page_dn", "Page -") +
        btn("page_up", "Page +") + btn("row_dn", "Row -") +
        btn("row_up", "Row +") +
      `</div>` +
      `<h4>Skill order</h4>` +
      `<div class="d" id="v_skills" style="margin-bottom:5px"></div>` +
      `<div class="g4" id="v_slots"></div>` +
      `<div style="margin-top:5px">` + btn("skill_clear", "Clear order") + `</div>` +
      // A SECOND ROTATION FOR THE HUNTS. Hunting House and Eudemon bosses are
      // a different fight from a story mission, so they carry their own order
      // - the same arrangement the reference bot uses (HHSkill, EudemonSkill).
      // Empty means "use the main order", which is why the label says so
      // rather than leaving the operator to infer it.
      `<h4>Hunt skill order (Hunting House / Eudemon)</h4>` +
      `<div class="d" id="v_hskills" style="margin-bottom:5px"></div>` +
      `<div class="g4" id="v_hslots"></div>` +
      `<div style="margin-top:5px">` + btn("hskill_clear", "Clear hunt order") + `</div>` +
      // EUDEMON BLACKLIST, with its own SCAN button directly above it.
      //
      // The scan belongs HERE rather than in the task row because it is what
      // fills the list underneath it - the two are one control surface, and
      // an operator looking at a stale or empty list should not have to know
      // that the fix lives somewhere else. It runs on click (`run_task`)
      // when nothing else is going, since it answers a question rather than
      // starting a shift.
      //
      // Every boss is clickable, event bosses included: the scan keeps this
      // list current, so the old SS lock protected nothing.
      `<h4>Eudemon bosses (click to skip)</h4>` +
      `<div style="margin-bottom:5px">` +
        btn("run_task", "Scan bosses now", "eudemon_scan") + `</div>` +
      `<div class="d" id="v_eu_none" style="margin-bottom:4px"></div>` +
      `<div class="g4" id="v_eudemon"></div>` +
      `<h4>View</h4><div style="margin-top:2px">` +
        btn("focus", "Focus mode") +
      `</div>` +
      `<div class="row" style="margin-top:6px"><span class="d">window</span>` +
        `<span id="v_viewport"></span></div>` +
      `<div class="g" id="v_viewports" style="margin-top:3px"></div>` +
      `<div class="d" id="v_vp_warn" style="margin-top:4px;display:none"></div>` +
      // RENDERER. Same shape as the window selector because it has the same
      // consequence: applying it reloads the game. The LIVE context is shown
      // beside the request, because asking for a backend is not getting one -
      // webgpu falls back where it is unavailable, and a WebGL blocklist drops
      // Ruffle to canvas2d without a word.
      `<div class="row" style="margin-top:6px"><span class="d">renderer</span>` +
        `<span id="v_renderer"></span></div>` +
      `<div class="g" id="v_renderers" style="margin-top:3px"></div>` +
      `<div class="d" id="v_rend_warn" style="margin-top:4px;display:none"></div>` +
      // SERVER. Same storage mechanism as the renderer (one localStorage key
      // the site reads on load) but a much bigger consequence: it points the
      // client at a different game HOST. The warning says so, and the list is
      // read from the game's own `servers` array rather than hardcoded.
      `<div class="row" style="margin-top:6px"><span class="d">server</span>` +
        `<span id="v_server"></span></div>` +
      `<div class="g" id="v_servers" style="margin-top:3px"></div>` +
      `<div class="d" id="v_srv_warn" style="margin-top:4px;display:none"></div>` +
      `<div style="margin-top:6px">` +
        btn("quit", "Quit (closes this panel)") +
      `</div>` +
      `<h4>Log</h4><div class="log" id="v_log"></div>`;
    el.querySelectorAll("[id^=v_]").forEach(n => { V[n.id] = n; });
    el.querySelector('[data-cmd="stop"]').classList.add("danger");
    el.querySelector('[data-cmd="quit"]').classList.add("danger");
  };

  // The task buttons are filled in SEPARATELY from the skeleton, and re-filled
  // whenever the list changes. The skeleton is built on the first render, and
  // the first render comes from the bootstrap with an EMPTY state object - so
  // building the task buttons there produced an empty Task section that nothing
  // ever repopulated. Keyed on the task list so this costs nothing per cycle.
  // The slot buttons never change, so build them once. Clicking one APPENDS it
  // to the order - that is what makes the order editable without a text field,
  // and it reads the same way the operator says it: "S1, then S3, then attack".
  const fillGrades = (grades) => {
    const key = (grades || []).join(",");
    if (!V.v_grades || V.v_grades.dataset.key === key) return;
    V.v_grades.dataset.key = key;
    V.v_grades.innerHTML = "";
    (grades || []).forEach(g => {
      const b = document.createElement("button");
      b.dataset.cmd = "grade"; b.dataset.arg = g;
      b.textContent = g === "auto" ? "Auto" : g;
      V.v_grades.appendChild(b);
    });
  };

  // Window sizes need CONFIRMING, because applying one reloads the game.
  // First press arms, second press within 6 s commits - so a stray click cannot
  // drop the session, and the operator is told what is about to happen.
  let armedVp = null, armedAt = 0;
  const fillViewports = (vps, current) => {
    const key = (vps || []).map(v => v.key).join(",") + "|" + (current || "");
    if (!V.v_viewports || V.v_viewports.dataset.key === key) return;
    V.v_viewports.dataset.key = key;
    V.v_viewports.innerHTML = "";
    (vps || []).forEach(v => {
      const b = document.createElement("button");
      b.dataset.vp = v.key;
      b.textContent = v.label;
      if (v.key === current) b.classList.add("on");
      b.addEventListener("click", ev => {
        ev.preventDefault(); ev.stopPropagation();
        const w = document.getElementById("v_vp_warn");
        const now = Date.now();
        if (armedVp === v.key && now - armedAt < 6000) {
          armedVp = null;
          if (w) { w.style.display = "block";
                   w.textContent = "applying " + v.label + " - reloading..."; }
          send("viewport", v.key);
          return;
        }
        armedVp = v.key; armedAt = now;
        if (w) {
          w.style.display = "block";
          w.textContent = "press " + v.label + " again to confirm - this "
                        + "RELOADS the game and returns to character select";
        }
      });
      V.v_viewports.appendChild(b);
    });
  };

  // Two-press confirm, exactly as the window selector does it: one press
  // arms and explains, a second within 6s commits. A single misclick must not
  // reload the game.
  let armedRend = null, armedRendAt = 0;
  const fillRenderers = (rs, current) => {
    const key = (rs || []).map(r => r.key).join(",") + "|" + (current || "");
    if (!V.v_renderers || V.v_renderers.dataset.key === key) return;
    V.v_renderers.dataset.key = key;
    V.v_renderers.innerHTML = "";
    (rs || []).forEach(r => {
      const b = document.createElement("button");
      b.dataset.rend = r.key;
      b.textContent = r.label;
      if (r.key === current) b.classList.add("on");
      b.addEventListener("click", ev => {
        ev.preventDefault(); ev.stopPropagation();
        const w = document.getElementById("v_rend_warn");
        const now = Date.now();
        if (armedRend === r.key && now - armedRendAt < 6000) {
          armedRend = null;
          if (w) { w.style.display = "block";
                   w.textContent = "switching to " + r.label + " - reloading..."; }
          send("renderer", r.key);
          return;
        }
        armedRend = r.key; armedRendAt = now;
        if (w) {
          w.style.display = "block";
          w.textContent = "press " + r.label + " again to confirm - this "
            + "RELOADS the game and returns to character select"
            + (r.key === "canvas"
               ? ". Canvas2D is far slower and every template here was "
               + "calibrated on a GL backend, so use it to diagnose, not to play."
               : "");
        }
      });
      V.v_renderers.appendChild(b);
    });
  };

  let armedSrv = null, armedSrvAt = 0;
  const fillServers = (svs, current) => {
    const key = (svs || []).map(v => v.index + ":" + v.name).join(",")
              + "|" + String(current);
    if (!V.v_servers || V.v_servers.dataset.key === key) return;
    V.v_servers.dataset.key = key;
    V.v_servers.innerHTML = "";
    (svs || []).forEach(v => {
      const b = document.createElement("button");
      b.dataset.srv = v.index;
      b.textContent = v.name;
      if (v.index === current) b.classList.add("on");
      b.addEventListener("click", ev => {
        ev.preventDefault(); ev.stopPropagation();
        const w = document.getElementById("v_srv_warn");
        const now = Date.now();
        if (armedSrv === v.index && now - armedSrvAt < 6000) {
          armedSrv = null;
          if (w) { w.style.display = "block";
                   w.textContent = "switching to " + v.name + " - reloading..."; }
          send("server", String(v.index));
          return;
        }
        armedSrv = v.index; armedSrvAt = now;
        if (w) {
          w.style.display = "block";
          w.textContent = "press " + v.name + " again to confirm. This is NOT "
            + "just a graphics setting - it reconnects the game to a different "
            + "HOST, and whether your character is there is not something the "
            + "bot can tell. It also reloads to character select.";
        }
      });
      V.v_servers.appendChild(b);
    });
  };

  // One filler for both banks - the hunt row is the same buttons sending a
  // different command, so a slot added to one list can never leak into the
  // other by copy-paste drift.
  const fillSlotRow = (host, slots, cmd) => {
    const key = (slots || []).join(",");
    if (!host || host.dataset.key === key) return;
    host.dataset.key = key;
    host.innerHTML = "";
    (slots || []).forEach(k => {
      const b = document.createElement("button");
      b.dataset.cmd = cmd; b.dataset.arg = k; b.textContent = k;
      host.appendChild(b);
    });
  };

  const fillSlots = (slots) => {
    fillSlotRow(V.v_slots, slots, "skill");
    fillSlotRow(V.v_hslots, slots, "hskill");
  };

  const fillEudemon = (list) => {
    const key = (list || []).map(e => e.key + ":" + (e.name || "")
        + (e.skipped ? "!" : "")).join(",");
    if (!V.v_eudemon || V.v_eudemon.dataset.key === key) return;
    V.v_eudemon.dataset.key = key;
    V.v_eudemon.innerHTML = "";
    setText("v_eu_none", (list || []).length ? ""
        : "(none seen yet - run the hunt once and they appear here)");
    (list || []).forEach(e => {
      const b = document.createElement("button");
      b.textContent = (e.name ? e.key + " " + e.name : e.key)
          + (e.skipped ? " x" : "");
      // EVERY boss is skippable now, event bosses included - the scan
      // keeps this list current, so the old SS lock protected nothing.
      b.dataset.cmd = "eu_skip"; b.dataset.arg = e.key;
      if (e.rank === "SS") b.title = "event boss - limited attempts";
      setOn(b, !!e.skipped);
      V.v_eudemon.appendChild(b);
    });
  };

  const fillTasks = (tasks) => {
    const key = (tasks || []).map(t => t.key).join(",");
    if (!V.v_tasks || V.v_tasks.dataset.key === key) return;
    V.v_tasks.dataset.key = key;
    V.v_tasks.innerHTML = "";
    (tasks || []).forEach(t => {
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
    window.__nsbotLastRender = Date.now();
    if (!V.v_state) skeleton(el, s);
    fillTasks(s.tasks);
    fillSlots(s.skill_slots);
    fillGrades(s.grades);
    fillViewports(s.viewports, s.viewport);
    setText("v_viewport", s.viewport_label || "");
    fillRenderers(s.renderers, s.renderer);
    fillServers(s.servers, s.server);
    setText("v_server", (() => {
      const cur = (s.servers || []).find(v => v.index === s.server);
      return cur ? cur.name : (s.server === null || s.server === undefined
                               ? "?" : String(s.server));
    })());
    setText("v_renderer",
            (s.renderer_live ? s.renderer_live : "?")
            + (s.renderer && s.renderer !== s.renderer_live
               ? " (asked " + s.renderer + ")" : ""));
    setText("v_grade", s.grade || "auto (best available)");
    setText("v_pin", s.pin || "highest unlocked");
    el.querySelectorAll('[data-cmd="grade"]').forEach(b =>
      setOn(b, b.dataset.arg === (s.grade || "auto")));
    fillEudemon(s.eudemon);
    const ord = (s.skills || []);
    setText("v_skills", ord.length
        ? ord.map((k, i) => `${i + 1}. ${k}`).join("   ")
        : "(none - Attack only)");
    const hord = (s.hunt_skills || []);
    setText("v_hskills", hord.length
        ? hord.map((k, i) => `${i + 1}. ${k}`).join("   ")
        : "(none - the main order above is used)");

    const mode = s.mode || "idle";
    setText("v_pill", mode,
            "pill " + (mode === "running" ? "run"
                     : mode === "paused" ? "pause"
                     : mode === "stopped" ? "stop"
                     : mode === "ready" ? "ready" : ""));
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

  // SAY SO WHEN NOTHING IS DRIVING THE PANEL. The dock is injected by a process
  // that then pushes state into it; if that process is not running, the panel
  // still renders - with stale values and dead buttons - and looks broken. It
  // has been reported as "the tasks are hidden" more than once, when in truth
  // there was simply no bot attached. A panel that cannot tell you it is
  // disconnected is worse than no panel.
  setInterval(() => {
    const el = document.getElementById(ID);
    if (!el) return;
    const stale = !window.__nsbotLastRender ||
                  (Date.now() - window.__nsbotLastRender) > 12000;
    el.classList.toggle("stale", stale);
    const w = document.getElementById("v_stale");
    if (w) w.style.display = stale ? "block" : "none";
  }, 2000);

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
            f"(window.__nsbotState = {payload},"
            f" window.__nsbotRender ? window.__nsbotRender({payload}) "
            f": 'no-panel')")

    def focus(self, on=True):
        """Hide the rest of the page so only the game (and this panel) shows."""
        return self.cdp.evaluate(
            f"(window.__nsbotFocus ? window.__nsbotFocus({str(bool(on)).lower()})"
            f" : 'no-panel')")

    def heartbeat(self):
        """Mark the panel alive, and report whether it still has its CONTENT.

        Returns "ok" normally, or "empty" if the panel is showing a bare
        skeleton - which happens whenever the page reloads: the browser re-runs
        the injected bootstrap on the new document by itself, and that first
        render has NO STATE, so the task buttons and every value are blank.
        Python is not involved in that re-injection and so never learns it
        happened; during a long mission only the heartbeat runs, so the panel
        sat empty-but-alive for the rest of the task.
        """
        return self.cdp.evaluate(
            "(window.__nsbotLastRender = Date.now(),"
            " (window.__nsbotState && Object.keys(window.__nsbotState).length)"
            "  ? 'ok' : 'empty')")

    def align(self):
        """Re-assert top alignment once the layout has settled."""
        return self.cdp.evaluate(
            "(window.__nsbotAlign ? window.__nsbotAlign() : 'no-panel')")

    def scroll_lock(self, on=True):
        """Remove the page scroll (or restore it). Renderer-independent.

        This is pure DOM, so it behaves identically on every Ruffle backend -
        which matters, because the operator asked for it "in all renders" and
        the per-renderer template work only covers what the SWF DRAWS.
        """
        return self.cdp.evaluate(
            f"(window.__nsbotScrollLock "
            f"? window.__nsbotScrollLock({str(bool(on)).lower()}) : 'no-panel')")

    def scroll_state(self):
        """(locked, snaps) read FROM THE PAGE, never remembered here.

        `snaps` counts how many times an actual scroll had to be undone, so an
        accidental wheel becomes something the log can name instead of a
        mystery drift.
        """
        try:
            raw = self.cdp.evaluate(
                "JSON.stringify([!!window.__nsbotScrollLocked,"
                " window.__nsbotScrollSnaps|0, window.scrollY|0])")
            import json as _j
            locked, snaps, y = _j.loads(raw)
            return bool(locked), int(snaps), int(y)
        except Exception:
            return False, 0, 0

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
