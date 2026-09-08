"""Cross-platform browser launch — the ONLY OS-specific code in the project.

Everything else (CDP, capture, perception, clicks) is portable. This module exists
so Windows and macOS differ in exactly one place: where the browser binary lives.

Three things it sets up, each for a measured reason:

  * --remote-debugging-port on loopback. The bot connects to 127.0.0.1, so CDP
    never leaves the machine. (This is why running locally beats the container:
    no bridge, no exposure.)
  * --user-data-dir, a DEDICATED profile. This is the credential store: you log in
    once and the session cookie persists. The bot never sees a password. It also
    keeps your everyday Chrome profile untouched.
  * a PINNED window size. Measured: text templates lose ~0.4 confidence at 8%
    scale error, so drifting geometry silently breaks detection.
"""
import json
import os, shutil, subprocess, sys, time, urllib.request

# Chrome first, Chromium as fallback. On Linux both are usually on PATH.
# ANY CHROMIUM-BASED BROWSER WORKS. The requirement was never Chrome - it is
# CDP, and every Chromium fork speaks it because it is Chromium's own protocol.
# Nothing in this project is Chrome-specific: `--remote-debugging-port`,
# `--app=`, `Input.dispatchMouseEvent`, `Runtime.addBinding` and
# `Page.addScriptToEvaluateOnNewDocument` are all Chromium features, so
# supporting Edge or Brave is a matter of finding the binary, not of writing
# code.
#
# FIREFOX AND SAFARI CANNOT BE MADE TO WORK BY ADDING A PATH HERE, and it is
# worth saying why rather than leaving someone to discover it:
#
#   * Firefox implemented a PARTIAL CDP shim, never the parts this bot depends
#     on, and is replacing it with WebDriver BiDi. Supporting it means writing
#     a second transport for capture, input and script injection - a rewrite of
#     cdp.py, act.py and capture.py, not a config change.
#   * Safari speaks the WebKit Inspector Protocol through `safaridriver`, which
#     is a different protocol again.
#
# So the honest answer to "I do not have Chrome" is "any of these will do", and
# a Chromium-based browser is a free download on every platform this runs on.
#
# Named, so the log can say WHICH browser it picked. An operator with four
# installed should not have to guess which one the bot took.
CANDIDATES = {
    "darwin": [
        ("Google Chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("Chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ("Microsoft Edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ("Brave", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        ("Vivaldi", "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"),
        ("Opera", "/Applications/Opera.app/Contents/MacOS/Opera"),
        ("Arc", "/Applications/Arc.app/Contents/MacOS/Arc"),
        ("Chrome Beta", "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"),
        ("Chrome Canary", "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
        # Per-user installs, which is where a machine without admin rights ends up.
        ("Google Chrome (user)", os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
        ("Microsoft Edge (user)", os.path.expanduser("~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")),
        ("Brave (user)", os.path.expanduser("~/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")),
    ],
    "win32": [
        ("Google Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ("Google Chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ("Microsoft Edge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ("Microsoft Edge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ("Brave", r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ("Brave", r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ("Vivaldi", r"C:\Program Files\Vivaldi\Application\vivaldi.exe"),
        ("Opera", os.path.join(os.environ.get("LOCALAPPDATA", ""),
                               r"Programs\Opera\opera.exe")),
        # Per-user installs. Edge is preinstalled on Windows 10/11, so in
        # practice a Windows machine almost always has SOMETHING here.
        ("Google Chrome (user)", os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                              r"Google\Chrome\Application\chrome.exe")),
        ("Chromium (user)", os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                         r"Chromium\Application\chrome.exe")),
        ("Vivaldi (user)", os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                        r"Vivaldi\Application\vivaldi.exe")),
    ],
    "linux": [
        # Snap and Flatpak put binaries outside a normal PATH often enough to
        # be worth naming explicitly.
        ("Chromium (snap)", "/snap/bin/chromium"),
        ("Google Chrome (flatpak)",
         "/var/lib/flatpak/exports/bin/com.google.Chrome"),
        ("Brave (flatpak)",
         "/var/lib/flatpak/exports/bin/com.brave.Browser"),
    ],
}

# Tried on PATH after the explicit paths above. Order is preference, not
# alphabetical: a stable Chrome or Chromium first, then the other forks.
PATH_NAMES = [
    ("Google Chrome", "google-chrome"),
    ("Google Chrome", "google-chrome-stable"),
    ("Chromium", "chromium"),
    ("Chromium", "chromium-browser"),
    ("Chrome", "chrome"),
    ("Microsoft Edge", "microsoft-edge"),
    ("Microsoft Edge", "microsoft-edge-stable"),
    ("Brave", "brave-browser"),
    ("Brave", "brave"),
    ("Vivaldi", "vivaldi"),
    ("Vivaldi", "vivaldi-stable"),
    ("Opera", "opera"),
]


def browser_name(path):
    """A human name for a binary path, for the log. Falls back to the filename."""
    for entries in CANDIDATES.values():
        for name, p in entries:
            if p and os.path.normcase(p) == os.path.normcase(path or ""):
                return name
    base = os.path.basename(path or "").lower()
    for name, exe in PATH_NAMES:
        if base == exe or base == exe + ".exe":
            return name
    return os.path.basename(path or "unknown")


def find_browser(explicit=None):
    if explicit:
        if not os.path.exists(explicit):
            raise FileNotFoundError(f"browser not found at {explicit}")
        return explicit
    plat = "win32" if sys.platform.startswith("win") else \
           "darwin" if sys.platform == "darwin" else "linux"
    for _name, p in CANDIDATES.get(plat, []):
        # ABSOLUTE ONLY. Several Windows entries are built from %LOCALAPPDATA%,
        # and if that is unset `os.path.join("", ...)` yields a RELATIVE path -
        # which `os.path.exists` would then resolve against the current working
        # directory. A launcher cd's into the bot's own folder, so a stray
        # `Google\Chrome\Application\chrome.exe` there would be picked up as
        # an installed browser.
        if p and os.path.isabs(p) and os.path.exists(p):
            return p
    for _name, exe in PATH_NAMES:
        found = shutil.which(exe)
        if found:
            return found
    tried = [p for _n, p in CANDIDATES.get(plat, []) if p]
    raise FileNotFoundError(
        f"No Chromium-based browser found on this {plat!r} machine.\n"
        f"Any of these work, because the bot needs CDP rather than Chrome "
        f"specifically: Chrome, Chromium, Edge, Brave, Vivaldi, Opera, Arc.\n"
        f"Firefox and Safari do NOT work - they speak different protocols "
        f"(WebDriver BiDi and the WebKit Inspector Protocol), which would need "
        f"a second transport written, not a setting changed.\n"
        f"If one IS installed somewhere unusual, point at it:\n"
        f"  engine/app.py --browser /path/to/the/binary\n"
        f"Looked in: {', '.join(tried) or '(PATH only)'}"
    )



# ---------------------------------------------------------------------------
# WHICH RENDERER IS ACTUALLY LIVE — because a fallback is SILENT
#
# Ruffle picks a renderer at startup and never announces it. Its ladder is
# `webgpu` -> `wgpu-webgl` -> `webgl` -> `canvas`, and `docs/BENCHMARK.md`
# already records the failure that matters: without the right Chrome flags the
# browser reports "WebGL1 blocklisted", WebGL is unavailable, and **Ruffle
# drops to canvas2d without a word**.
#
# That is not a performance footnote for this bot. EVERY TEMPLATE AND EVERY
# COLOUR THRESHOLD IN THIS PROJECT WAS CALIBRATED AGAINST ONE RENDERER. A
# different one draws the same SWF with different filtering and antialiasing,
# so a silent switch changes the pixels underneath a perception layer that has
# no idea it happened - and the symptom is "the bot suddenly stopped
# recognising things", with nothing in the log to explain it. A driver update
# or a blocklist change is enough to trigger it.
#
# So: read it, log it, and say plainly when it is not what was asked for. The
# probe cannot disturb anything - asking a canvas for a context it does not
# have returns null rather than creating one.
#
# Ruffle 0.2 keeps its canvas inside the custom element's SHADOW ROOT, so a
# document-level `querySelector('canvas')` finds nothing at all. That is why
# the first version of this probe reported zero canvases on a perfectly
# healthy page.
RENDERER_PROBE = """
(() => {
  const out = {requested: null, live: null, where: null};
  try {
    const f = document.querySelector('iframe[src*="emulator"], iframe[src*="play"]');
    const d = f ? f.contentDocument : document;
    const w = f ? f.contentWindow : window;
    // REPORT WHAT RUFFLE USED, not what we asked for. `loadedConfig` is the
    // merged config the player actually loaded with; the global is only a
    // default and echoing it back reported our own override as though it were
    // the site's choice.
    if (w && w.RufflePlayer && w.RufflePlayer.config)
      out.global_pref = w.RufflePlayer.config.preferredRenderer || null;
    const p = d && d.querySelector('ruffle-player');
    if (!p) { out.where = 'no ruffle-player'; return JSON.stringify(out); }
    const sr = p.shadowRoot;
    const cv = sr ? sr.querySelector('canvas') : d.querySelector('canvas');
    if (!cv) { out.where = 'no canvas yet'; return JSON.stringify(out); }
    out.where = sr ? 'shadow root' : 'document';
    out.size = [cv.width, cv.height];
    try {
      out.requested = (p.loadedConfig && p.loadedConfig.preferredRenderer) || null;
    } catch (e) {}
    for (const t of ['webgl2', 'webgl', '2d', 'bitmaprenderer']) {
      try { if (cv.getContext(t)) { out.live = t; break; } } catch (e) {}
    }
  } catch (e) { out.where = 'error: ' + String(e).slice(0, 80); }
  return JSON.stringify(out);
})()
"""

# A live context of "2d" means Ruffle fell back to canvas2d. Everything the
# perception layer believes was measured on a GL backend.
GL_CONTEXTS = ("webgl2", "webgl")


def renderer_info(cdp):
    """What Ruffle was asked for and what it actually got. Never raises."""
    try:
        raw = cdp.evaluate(RENDERER_PROBE)
        if not raw:
            return {}
        import json as _json
        return _json.loads(raw)
    except Exception:
        return {}


def describe_renderer(info):
    """One log line, and whether it deserves a warning.

    Returns (message, is_warning).
    """
    if not info:
        return "renderer: could not be read", False
    req, live, where = info.get("requested"), info.get("live"), info.get("where")
    size = info.get("size")
    if live is None:
        return f"renderer: no canvas context yet ({where})", False
    # The CONTEXT TYPE is a coarse signal: both `wgpu-webgl` and `webgl` draw
    # through a WebGL2 context, so it separates GL from canvas2d and nothing
    # finer. The backend actually in use comes from loadedConfig.
    base = f"renderer: {req or 'unknown'}"
    if live:
        base += f" (context {live})"
    if size:
        base += f", canvas {size[0]}x{size[1]}"
    if live not in GL_CONTEXTS:
        return (base + " - THIS IS THE CANVAS2D FALLBACK. Every template and "
                       "colour threshold here was calibrated on a GL backend, "
                       "so matches may be off and the frame rate will be far "
                       "lower. Check Chrome's WebGL blocklist.", True)
    return base, False


# ---------------------------------------------------------------------------
# CHOOSING THE RENDERER
#
# Ruffle reads `preferredRenderer` when the player is CONSTRUCTED, and the site
# assigns its own config first (measured: it asks for "wgpu-webgl"). So an
# override has to be installed with `Page.addScriptToEvaluateOnNewDocument`,
# which runs before page scripts, AND re-asserted on a short interval - because
# the site's assignment lands after ours and would otherwise win.
#
# It only takes effect on the NEXT document, so switching means a reload. A
# reload keeps the game session (only quitting the browser signs it out), so
# this is safe, but it does drop to character select and the resume ladder has
# to climb back.
#
# ASKING IS NOT GETTING. `webgpu` falls back where WebGPU is unavailable, and
# `docs/BENCHMARK.md` records Chrome blocklisting WebGL and Ruffle dropping to
# canvas2d silently. So always read `renderer_info` afterwards and report the
# LIVE context rather than the request.
RENDERER_BACKENDS = ("webgpu", "wgpu-webgl", "webgl", "canvas")

# ---------------------------------------------------------------------------
# THE GAME'S OWN SETTINGS — three localStorage keys, read on every load
#
# THREE ATTEMPTS AT THIS WERE AIMED AT THE WRONG LAYER, and the record is worth
# keeping. Patching `RufflePlayer.config.preferredRenderer` lost, because a
# per-load config overrides the global. Wrapping the element's `load(options)`
# did nothing, because the site calls `player.load(swfUrl)` with a bare STRING.
# And that prototype wrap never even applied. Measured throughout:
#
#     RufflePlayer.config.preferredRenderer  = "webgl"       <- our override
#     player.loadedConfig.preferredRenderer  = "wgpu-webgl"  <- what Ruffle used
#
# so asking for `canvas` still produced a WebGL2 context and the operator
# correctly reported that nothing had changed.
#
# The site already has a control for all of it - a gear icon beside the game,
# which FOCUS MODE HIDES, which is why it was never seen. Its emulator page
# reads, on every load:
#
#     const defaultRender  = isIOS ? 'webgl' : 'wgpu-webgl';
#     const savedRender    = localStorage.getItem('renderMode')  || defaultRender;
#     const savedQuality    = localStorage.getItem('gameQuality') || 'high';
#     var   selected_server = parseInt(localStorage.getItem('ns_server_index') || '0', 10);
#     const render         = urlParams.get('render') || savedRender;
#
# So a setting is ONE localStorage key plus a reload. No config patching, no
# race with the site's own assignment, and the site caches and reuses it for
# us - which is strictly better than keeping our own copy, because a second
# copy would be a stale duplicate of the truth. This project has been bitten by
# exactly that before: a cached belief about page state that a navigation
# invalidated.
#
# GENERAL LESSON, and it cost three failed attempts: when a site already has a
# control for something, FIND ITS CONTROL before reverse-engineering the
# runtime. The operator naming the gear was the fastest step in the whole
# investigation.
#
# The keys live in the page's origin and are SHARED with the game iframe.
# `iframe.contentWindow.localStorage === window.localStorage` is FALSE - each
# window gets its own Storage object - but the data is the same store, verified
# by writing in one and reading it in the other. Do not use that identity
# comparison as an origin test; it proves nothing.
GAME_SETTING_KEYS = ("renderMode", "gameQuality", "ns_server_index")


def read_game_settings(cdp):
    """The game's stored settings, plus the server list it offers.

    Returns {} on any failure - never a guess, because a wrong value here
    would be shown to the operator as the current state.
    """
    src = """
    (() => {
      const out = {settings: {}, servers: [], selected_server: null,
                   default_render: null};
      try {
        for (const k of %s) out.settings[k] = localStorage.getItem(k);
      } catch (e) {}
      try {
        const f = document.querySelector('iframe[src*="emulator"], iframe[src*="play"]');
        const w = f ? f.contentWindow : window;
        // The server list is a plain var on the emulator page, so read the
        // real one rather than hardcoding names that could drift.
        out.servers = (w.servers || []).map((s, i) => ({
            index: i, name: s.name || ("Server " + (i + 1)) }));
        out.selected_server = (typeof w.selected_server === 'number')
            ? w.selected_server : null;
        out.default_render = w.isIOS ? 'webgl' : 'wgpu-webgl';
      } catch (e) {}
      return JSON.stringify(out);
    })()
    """ % json.dumps(list(GAME_SETTING_KEYS))
    try:
        raw = cdp.evaluate(src)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def write_game_setting(cdp, key, value):
    """Store one setting the way the site's own gear does. True if it stuck.

    Verified by reading the key back: a write that silently failed would leave
    the operator believing a switch had been made, which is exactly the failure
    this replaced.
    """
    if key not in GAME_SETTING_KEYS:
        raise ValueError(f"unknown game setting {key!r}; "
                         f"expected one of {GAME_SETTING_KEYS}")
    src = """
    (() => {
      try {
        localStorage.setItem(%s, %s);
        return String(localStorage.getItem(%s));
      } catch (e) { return "ERR " + String(e).slice(0, 60); }
    })()
    """ % (json.dumps(key), json.dumps(str(value)), json.dumps(key))
    try:
        got = cdp.evaluate(src)
    except Exception:
        return False
    return got == str(value)


def cdp_ready(port, timeout=1.5):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout)
        return True
    except Exception:
        return False


def launch(url, profile_dir, port=9222, window=(1728, 994), browser=None,
           extra_flags=(), reuse=True, app_mode=False):
    """Start a dedicated browser instance and wait for CDP.

    If `reuse` and something is already serving CDP on `port`, attach to that
    instead of starting a second instance.

    `app_mode` passes `--app=<url>` instead of the URL as a positional argument.
    Chrome then opens a window with no tab strip, no omnibox and no bookmarks
    bar - a plain application window. Combined with `engine/dock.py`, which
    injects the control panel into the page itself, that is the whole of "a
    native-looking bot window": the operator sees the game rendering at its own
    framerate with the controls beside it, and nothing is being streamed.

    Worth being clear about what this is NOT: it does not embed the game in some
    other runtime. It cannot - Ruffle is WASM + WebGL, the session cookie lives
    in this profile, and CDP is how we click. The reference bot's "native" shell
    is Adobe AIR + CEF, which is a Chromium in a frame; this is the same trade
    without the extra runtime.
    """
    if reuse and cdp_ready(port):
        # Alive, but it may have no pages left (window closed). Opening a target
        # is far friendlier than a 30s timeout the caller cannot interpret.
        try:
            import json as _json
            raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5).read()
            if not [t for t in _json.loads(raw) if t.get("type") == "page"]:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/new?" + url, timeout=10).read()
        except Exception:
            pass
        return None, port

    exe = find_browser(browser)
    os.makedirs(profile_dir, exist_ok=True)

    # docker rm -f / SIGKILL leaves these behind and the next launch refuses to
    # start, claiming the profile is in use. Same failure mode applies to a hard
    # kill locally, so clear them defensively.
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.unlink(os.path.join(profile_dir, lock))
        except OSError:
            pass

    args = [
        exe,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",   # loopback only, never exposed
        f"--user-data-dir={profile_dir}",
        f"--window-size={window[0]},{window[1]}",
        "--window-position=0,0",
        "--no-first-run",
        "--no-default-browser-check",
        "--autoplay-policy=no-user-gesture-required",
        # Ruffle's render loop is requestAnimationFrame-driven and Chrome
        # suppresses rAF entirely for hidden/occluded windows (measured: 0
        # callbacks per 1500ms). These keep it rendering when the window is not
        # frontmost, so the machine stays usable.
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        *extra_flags,
        (f"--app={url}" if app_mode else url),
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + 30
    while time.time() < deadline:
        if cdp_ready(port):
            return proc, port
        if proc.poll() is not None:
            raise RuntimeError(f"browser exited immediately (code {proc.returncode})")
        time.sleep(0.5)
    raise TimeoutError(f"CDP not reachable on 127.0.0.1:{port} after 30s")


def pin_viewport(cdp, width=1728, height=851, scale=2):
    """Force an exact viewport and device pixel ratio.

    More reliable than --window-size alone: window size includes browser chrome,
    which differs by platform and version, so the resulting viewport drifts.
    Overriding device metrics makes the canvas geometry - and therefore template
    scale - reproducible on Windows and macOS alike.
    """
    cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
             deviceScaleFactor=scale, mobile=False)
    return cdp.evaluate(
        "JSON.stringify({w: innerWidth, h: innerHeight, dpr: devicePixelRatio})")
