"""In-page overlay panel, injected into the game tab over CDP.

Why this instead of streaming JPEGs to a separate web page:

  * A page on our own origin CANNOT read or click a cross-origin iframe
    (same-origin policy; no CORS header changes that). So embedding the game in
    our dashboard was never going to work.
  * Streaming captured frames DOES work but adds visible lag, because the view
    then depends on our capture+encode+poll cycle.

Inverting it removes the problem entirely: the operator watches the real game
window rendering natively, and we inject a panel into that same document. No
streaming, no iframe boundary, no lag.

This only adds a positioned <div> to the page. It does not touch the SWF, the
game's own DOM, or any game logic.
"""
import json

PANEL_ID = "__nsbot_overlay"

_CSS = """
#__nsbot_overlay{position:fixed;top:8px;right:8px;width:300px;z-index:2147483647;
  font:11px/1.4 ui-monospace,Menlo,monospace;color:#e6e9ef;
  background:rgba(20,22,28,.92);border:1px solid #2b313d;border-radius:8px;
  padding:8px 10px;pointer-events:none;box-shadow:0 4px 18px rgba(0,0,0,.5)}
#__nsbot_overlay h4{margin:0 0 6px;font-size:10px;letter-spacing:.09em;
  text-transform:uppercase;color:#8b94a7;font-weight:600;
  display:flex;justify-content:space-between}
#__nsbot_overlay .r{display:flex;justify-content:space-between;gap:8px}
#__nsbot_overlay .d{color:#8b94a7}
#__nsbot_overlay .hit{color:#4ade80}
#__nsbot_overlay .warn{color:#fbbf24}
#__nsbot_overlay .bad{color:#f87171}
#__nsbot_overlay .acc{color:#60a5fa}
#__nsbot_overlay hr{border:0;border-top:1px solid #2b313d;margin:6px 0}
"""

# Creates the panel once, then only rewrites its contents.
_BOOTSTRAP = """
(() => {
  if (document.getElementById(%(id)r)) return "exists";
  const st = document.createElement('style'); st.textContent = %(css)r;
  document.documentElement.appendChild(st);
  const d = document.createElement('div'); d.id = %(id)r;
  document.documentElement.appendChild(d);
  return "created";
})()
"""

_UPDATE = """
(() => {
  const el = document.getElementById(%(id)r);
  if (!el) return "missing";
  const s = %(payload)s;
  const cls = a => a==='click'||a==='abort' ? 'acc' : (a==='halt' ? 'bad' : 'd');
  el.innerHTML =
    `<h4><span>NS BOT</span><span class="${s.live?'hit':'d'}">${s.mode}</span></h4>
     <div class="r"><span class="d">state</span><span class="${s.state==='unknown'?'warn':'hit'}">${s.state}</span></div>
     <div class="r"><span class="d">cycle</span><span>${s.cycle}</span></div>
     <div class="r"><span class="d">scoring</span><span>${s.score_ms} ms</span></div>
     <hr>
     <div class="r"><span class="d">action</span><span class="${cls(s.action)}">${s.action||'-'}</span></div>
     <div class="d" style="margin-top:2px">${s.reason||''}</div>
     ${s.watchdog && s.watchdog!=='-' ? `<div class="r" style="margin-top:4px"><span class="d">watchdog</span><span class="${['stalled','regenerating'].includes(s.watchdog)?'bad':'d'}">${s.watchdog}</span></div>`:''}
     ${s.bars && s.bars.length ? '<hr>'+s.bars.map(b=>`<div class="r"><span class="d">enemy y=${b[0]}</span><span>${b[1].toFixed(1)}%%</span></div>`).join(''):''}
     <hr>
     ${(s.templates||[]).map(t=>`<div class="r"><span class="${t[2]?'hit':'d'}">${t[2]?'●':'○'} ${t[0]}</span><span class="${t[2]?'hit':'d'}">${t[1].toFixed(3)}</span></div>`).join('')}`;
  return "ok";
})()
"""


def ensure(cdp):
    """Create the panel if the page does not already have one."""
    return cdp.evaluate(_BOOTSTRAP % {"id": PANEL_ID, "css": _CSS},
                        await_promise=False)


def update(cdp, *, mode, live, state, cycle, score_ms, action, reason,
           watchdog="-", bars=(), templates=()):
    payload = {
        "mode": mode, "live": bool(live), "state": state, "cycle": cycle,
        "score_ms": score_ms, "action": action or "-", "reason": reason or "",
        "watchdog": watchdog,
        "bars": [[int(y), float(p)] for y, p in bars][:4],
        "templates": [[n, float(c), bool(h)] for n, c, h in templates][:6],
    }
    return cdp.evaluate(_UPDATE % {"id": PANEL_ID, "payload": json.dumps(payload)},
                        await_promise=False)


def remove(cdp):
    """Delete the injected panel from the page.

    Needed because `ensure()` puts a real element in the game page's DOM, and
    that element survives simply stopping the updates — it stays there, stale,
    occluding whatever is behind it. Since the page we inject into is also the
    page we screenshot for perception, a leftover panel is not merely cosmetic:
    it covers part of the frame the matcher reads (it sat over the gold/token
    HUD in testing). So turning the overlay off has to actively remove it.
    """
    try:
        cdp.evaluate(
            "(()=>{const e=document.getElementById('%s');"
            "if(e){e.remove();return 'removed';}return 'absent';})()" % PANEL_ID)
        return True
    except Exception:
        return False
