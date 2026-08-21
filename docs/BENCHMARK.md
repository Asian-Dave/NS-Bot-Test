# Architecture benchmark — host Metal vs containerized SwiftShader

Run 2026-08-21 on Apple M5 Pro, Docker 29.6.2 (arm64, 18 CPU, 20.9 GB).
Workload: `bench/bench.js` — 1500 textured draw calls per frame into a 960x839
canvas (the real game canvas size), unpaced, `gl.finish()` per frame.

## Results

| Metric | Host (Chrome, Metal) | Container (Chromium, SwiftShader) | Ratio |
|---|---|---|---|
| GL renderer | ANGLE Metal, Apple M5 Pro | ANGLE Vulkan SwiftShader (LLVM 16) | — |
| GL fps | **4513.18** | **24.62** | 183x |
| Draw calls/sec | 6,769,774 | 36,927 | 183x |
| rAF delivered | 0.42 (tab hidden) | 17.83 (visible) | — |
| CPU ops/sec | 2.23e9 | 2.32e9 | ~1.0x |
| Headroom vs 24 fps | **188x** | **1.03x** | — |

## RESOLVED — real Ruffle, measured

The synthetic numbers above were misleading. Measured with the actual game:

| Container config | Real Ruffle fps | Verdict |
|---|---|---|
| vsync on (default) | 17.32 | BELOW 24 (0.72x) |
| **vsync off** | **49.68** | **CLEARS 24 (2.07x)** |

The ~17fps was **not** SwiftShader running out of capacity. It was frame pacing.
Diagnosis: the synthetic bench reported rAF 17.83 and real Ruffle 17.32 - two wildly
different workloads landing within 3%, which is the signature of a fixed ceiling
rather than a load-dependent slowdown. Confirmed by measuring bare rAF with **no
workload at all**: still 15.08 fps. Adding `--disable-gpu-vsync
--disable-frame-rate-limit` took bare rAF to 59.29 and real Ruffle to 49.68.

Correction to an earlier reading: real Ruffle now *exceeds* the synthetic 24.62 fps,
so its true per-frame draw load is **lighter** than the 1500 draw calls I assumed,
not heavier.

### Decision

**Fully containerized.** Zero host dependencies, host desktop stays free, rAF can
never be suppressed (Xvfb window is always visible), and 2x headroom means this is
viable for combat as well as static flows.

### Two operational gotchas, both load-bearing

1. `--enable-unsafe-swiftshader --use-gl=angle --use-angle=swiftshader` - without
   these, `WebGL1 blocklisted` and Ruffle silently drops to canvas2d.
2. `--disable-gpu-vsync --disable-frame-rate-limit` - without these you get 17fps
   and it looks like the GPU is too slow when it is not.

Also: `docker rm -f` SIGKILLs Chromium so it never releases `SingletonLock` in the
profile volume. The next container has a different hostname and Chromium refuses to
start, claiming the profile is in use "on another computer". `start.sh` now clears
the stale lock files on boot.

## Conclusions (from the synthetic pass)

1. **CPU is a non-issue.** Parity, because Docker Desktop runs arm64 natively on
   Apple Silicon. Ruffle's AVM interpretation will not be the bottleneck.
2. **GPU is the whole story.** SwiftShader is 183x slower on draw calls.
3. **The container clears 24 fps by only 3%.** That is not headroom, it is a
   rounding error. And `rAFDeliveredFps` was already only 17.83 in the container
   *while visible* — under 24 — which suggests the compositor could not sustain the
   target with the GL load running.
4. **Caveat, and it matters:** 1500 draw calls/frame is my estimate of Ruffle's
   profile, not a measurement of it. Real Ruffle drawing the village scene with
   animated NPCs may well exceed that, which would push the container under 24 fps.
   I did not measure real Ruffle in a container — that needs a logged-in session
   inside it, which I will not create.

## rAF suppression (measured, separate finding)

A **hidden** Chrome tab delivers **0 rAF callbacks in 1500 ms** — fully suppressed,
not throttled to 1 Hz. Confirmed via `document.visibilityState: "hidden"`,
`hidden: true`, `hasFocus: false`. So Ruffle stops dead in a background tab.

Two consequences:
* The Xvfb container topology sidesteps this entirely — the window is always visible
  (`visibility: visible` in the container run), so rAF always flows.
* On the host, a **screen capture appears to force a frame**: the first benchmark
  advanced only in bursts, exactly when screenshotted. If that holds, a
  capture-driven bot could keep a hidden tab progressing at roughly its capture
  rate. Unverified — worth testing before relying on it.

## Recommendation: split by behaviour, not one global choice

The 24 fps bar only applies to behaviours that need smooth animation.

| Behaviour | Needs | Verdict |
|---|---|---|
| Daily reward claiming, popup drains, static menus | detect a static popup, click it. A few fps is ample. | **Container is fine** — gives the fully-dockerized setup with zero host deps |
| Combat / cooldown timing | real-time reaction at ~24 fps | **Needs host Metal** (188x headroom) via CDP |

Phase 4 targets daily rewards, so the container is the right runtime *now*, and it
is what was asked for. Revisit for combat, when the timing requirement is real.
