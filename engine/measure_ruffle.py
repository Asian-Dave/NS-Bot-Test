"""Measure the REAL Ruffle frame rate for Ninja Saga, from inside the container.

This closes the caveat in docs/BENCHMARK.md. The synthetic benchmark said the
container clears 24fps by only 3%, but that used *my estimate* of Ruffle's draw-call
profile (1500/frame) rather than a measurement. This script measures the actual
thing: how fast Ruffle advances frames while rendering the real game.

Requires someone to have logged in via noVNC first - the script refuses to guess.
"""
import json, sys, time
from cdp import CDP, find_page_target, CDPError

SAMPLE_SECONDS = 6

# Locate the same-origin game iframe, then count rAF callbacks in ITS window -
# that is the loop Ruffle drives rendering from.
PROBE_INSTALL = """
(() => {
  const f = [...document.querySelectorAll('iframe')]
    .find(x => x.contentDocument && x.contentDocument.querySelector('ruffle-player'));
  if (!f) return JSON.stringify({ready: false, reason: 'no ruffle iframe yet'});
  const rp = f.contentDocument.querySelector('ruffle-player');
  const cv = rp.shadowRoot && rp.shadowRoot.querySelector('canvas');
  window.__m = {ticks: 0, t0: performance.now()};
  (function loop(){ window.__m.ticks++; f.contentWindow.requestAnimationFrame(loop); })();
  // What renderer did Chromium actually hand out? Confirms SwiftShader is live.
  let glr = 'n/a';
  try {
    const p = document.createElement('canvas').getContext('webgl');
    const d = p && p.getExtension('WEBGL_debug_renderer_info');
    if (d) glr = p.getParameter(d.UNMASKED_RENDERER_WEBGL);
  } catch (e) {}
  return JSON.stringify({
    ready: true,
    ruffleReadyState: rp.readyState,
    canvasBacking: cv ? (cv.width + 'x' + cv.height) : null,
    visibility: document.visibilityState,
    glRenderer: glr
  });
})()
"""

READ = """JSON.stringify({ticks: window.__m.ticks,
                          seconds: (performance.now() - window.__m.t0) / 1000})"""


def main():
    t = find_page_target(url_contains="ninjasaga", timeout=60)
    c = CDP(t["webSocketDebuggerUrl"])
    c.call("Page.enable")

    # Wait for the operator to reach a state where the game is actually running.
    info = None
    for _ in range(60):
        info = json.loads(c.evaluate(PROBE_INSTALL, await_promise=False))
        if info.get("ready"):
            break
        time.sleep(2)
    if not info or not info.get("ready"):
        print(json.dumps({"error": "ruffle not running - log in via noVNC first",
                          "detail": info}, indent=2))
        return 1

    time.sleep(SAMPLE_SECONDS)
    r = json.loads(c.evaluate(READ, await_promise=False))
    fps = r["ticks"] / r["seconds"] if r["seconds"] else 0

    out = {
        "glRenderer": info["glRenderer"],
        "visibility": info["visibility"],
        "ruffleReadyState": info["ruffleReadyState"],
        "canvasBacking": info["canvasBacking"],
        "sampleSeconds": round(r["seconds"], 2),
        "frames": r["ticks"],
        "ruffleFps": round(fps, 2),
        "swfTargetFps": 24,
        "verdict": "CLEARS 24fps" if fps >= 24 else "BELOW 24fps",
        "headroomVs24": round(fps / 24, 2),
    }
    print(json.dumps(out, indent=2))
    c.screenshot("/tmp/ruffle_state.png")
    print("screenshot -> /tmp/ruffle_state.png", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
