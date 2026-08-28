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
CANDIDATES = {
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ],
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Google\Chrome\Application\chrome.exe"),
    ],
    "linux": [],   # resolved via PATH below
}


def find_browser(explicit=None):
    if explicit:
        if not os.path.exists(explicit):
            raise FileNotFoundError(f"browser not found at {explicit}")
        return explicit
    plat = "win32" if sys.platform.startswith("win") else \
           "darwin" if sys.platform == "darwin" else "linux"
    for p in CANDIDATES.get(plat, []):
        if p and os.path.exists(p):
            return p
    for exe in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(exe)
        if found:
            return found
    raise FileNotFoundError(
        f"no Chrome/Chromium found for platform {plat!r}. "
        "Pass --browser with an explicit path.")


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
