> **Superseded.** This describes the fully-containerized topology (game + bot inside Docker), which was measured and then dropped in favour of running the bot locally against the host browser over loopback CDP. Kept for the benchmark rationale in `BENCHMARK.md`; see `CLAUDE.md` for the architecture actually in use.

# Container runtime — how to run it

Everything Python lives in the container. **Nothing is installed on the host** —
no `mss`, no `opencv`, no `pyobjc`, no venv, and macOS Screen Recording permission
is never needed, because we never capture a screen. We capture the page over CDP.

## Images

| Image | Purpose |
|---|---|
| `ns-bench:arm64` | synthetic GL benchmark (host vs SwiftShader) |
| `ns-runtime:arm64` | the real runtime: Chromium + Xvfb + noVNC + the engine |

## Start it

```sh
docker run -d --name ns \
  -p 127.0.0.1:6080:6080 \
  -v ns-profile:/profile \
  ns-runtime:arm64
```

* `-p 127.0.0.1:6080:6080` binds noVNC to **localhost only**. It is deliberately not
  reachable from your network — x11vnc runs with no password inside the container,
  so it must not be exposed. Do not change this to `-p 6080:6080`.
* `-v ns-profile:/profile` persists Chromium's profile, so **you log in once** and
  the session survives restarts.

Then open: **http://localhost:6080/vnc.html**

You will see the container's virtual display with Chromium already on the game.
Log in yourself — I will not enter credentials, and session tokens are not copied
in from the host.

## Why a virtual display rather than your own Chrome

Measured: a **hidden** Chrome tab delivers **0 requestAnimationFrame callbacks per
1500 ms**. Fully suppressed, not throttled. Ruffle stops dead in a background tab.

On an Xvfb display the browser window is always visible and always the active tab,
so rAF always flows and your real desktop stays free. That is the entire reason for
this topology.

## Measure real Ruffle throughput

Once you are logged in and the game is running:

```sh
docker exec -w /engine ns python3 measure_ruffle.py
```

Reports actual Ruffle fps against the SWF's 24fps target, plus the live GL renderer
string so you can confirm SwiftShader is really in use.

## The three mandatory Chromium flags

```
--enable-unsafe-swiftshader --use-gl=angle --use-angle=swiftshader
```

Without these, current Chromium reports `WebGL1 blocklisted`, WebGL is unavailable,
and Ruffle silently falls back to its far slower canvas2d renderer. This would look
like a mysterious performance problem rather than a configuration one.

## Stop / reset

```sh
docker rm -f ns              # stop (profile and login survive)
docker volume rm ns-profile  # forget the login too
```
