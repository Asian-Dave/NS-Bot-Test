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
