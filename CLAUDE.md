# Ninja Saga UI-automation bot — project knowledge

Read this before touching perception, geometry, or the combat loop. Every entry below
was established by measurement against the live game, and several of them contradict
what seemed obvious at first. The "Hard-won corrections" section exists because those
mistakes each cost real debugging time.

## Architecture (decided, with data)

**Bot runs locally in Python; the game runs in the user's own Chrome. Communication
is CDP over loopback.**

* Capture = `Page.captureScreenshot`. NOT `mss` — we capture the *page*, not the
  screen, so no Screen Recording permission and the window need not be frontmost.
* Clicks = `Input.dispatchMouseEvent`. NOT `pydirectinput` (Windows-only; it runs
  `ctypes.windll.user32.SendInput` at import so it cannot even be imported on macOS).
  Verified: injected events reach the Ruffle canvas with `isTrusted: true`.
* Dependencies: **`opencv-python-headless` only.** numpy already present. `cdp.py`,
  `act.py` and `browser.py` are pure stdlib.
* Docker was evaluated and dropped. Keeping the game in the host browser removes the
  container→host CDP bridge, which would have required exposing CDP beyond loopback
  (= full browser control, every cookie, to anything that can reach the port).
* Cross-platform: the ONLY OS-specific code is `browser.py`'s binary-path lookup.

Benchmarks, if the container question is ever reopened: host Chrome renders at 120fps
with 188x GL headroom; a SwiftShader container reaches 49.68fps *only* with
`--disable-gpu-vsync --disable-frame-rate-limit` (17.32fps without). The SWF targets
24fps, so both clear it. See `docs/BENCHMARK.md`.

## Geometry — read this before changing any sizing

The game is a Flash SWF on **Ruffle** (WASM + WebGL) drawing into one canvas. There
are **no DOM elements inside the game**; template matching is the only interface.

**NEVER resize the `ruffle-player` element via CSS.** Doing so desyncs click →
stage coordinate mapping inside the SWF. Forcing 960x839 (real stage is 960x**720**)
made the game stop responding to clicks and looked exactly like a hang. Use
`Emulation.setDeviceMetricsOverride(w, h, deviceScaleFactor)` instead — it pins the
real viewport so the SWF scales naturally.

Two valid topologies:

| | canvas | notes |
|---|---|---|
| iframe on `/play` | 960x839 CSS, fixed | sticky page header **covers the top ~60 CSS px** |
| top-level `/emulator` | fills viewport | no header, canvas at (0,0) — preferred |

The `/emulator` URL is read at runtime from the iframe's `src`. **It contains a
live session token** (`fb_at`, `fb_sig`) and is time-signed (`time`, `hash_time`,
`_cb`). Never persist it, never log it, never commit it. Prefer `location.replace()`
or CDP navigation so it does not enter browser history.

Transform, when the game is an iframe inside the page:
`iframe_local_css = screenshot_xy / screenshot_scale - (iframe_x, iframe_y)`;
`native_px = css * dpr`. Verified to within 1px. `screenshot_scale` is
`min(1, 1568/innerWidth)` for MCP screenshots.

## Perception

**Calibrate every mask and threshold against reference extremes. Do not eyeball
colour ranges.** A single loose HP mask caused three consecutive wrong conclusions.

Measured-good HP bar fill (BGR, bright red only — must exclude the dark empty track):

```python
LO = (0, 0, 140); HI = (70, 70, 255)
```

Template thresholds are calibrated in `configs/daily_reward.json` as measured peak
minus 0.07 (`engine/calibrate.py` re-derives them). Median peak 0.973 across 16
templates. `claim_daily` is the outlier at 0.808 — probably an animated gloss on the
button; **re-measure it against a live capture** before trusting it.

Scale sensitivity is content-dependent and matters a lot:

* text-heavy templates lose **~0.4 confidence at 8% scale error**
  (`loading_text` 0.489, `hunting_house_btn` 0.445, `wish_btn` 0.440)
* round blobs barely care (`close_popup_x_menu` 0.048, `day_current_pointer` 0.057)

So geometry must be pinned, and `cv2.matchTemplate` is not scale-invariant.

Prefer pixel reads over OCR: HP/CP bars via `bar_fill_ratio`, cooldowns via
`is_desaturated` (mean HSV saturation), numbers via digit templates if ever needed
(Ruffle rasterises deterministically, so digit templates beat OCR).

Bad template targets: **semi-transparent labels over animated art** (the village
"Hunting House"/"Battle" labels), and **enemy name plates** (names vary per
encounter: Escaped Prisoner / Criminal / Desert Clawman).

## Combat model

* **Turn gate = command-bar presence.** If `Attack`/`Dodge`/`Charge`/`Run` are
  visible, it is your turn. The turn-order marker reaching `Action!` is NOT a
  reliable gate, and the Victory panel draws over the bar — so check states in
  priority order (result panel BEFORE combat input), never by presence alone.
* Static frames are **normal** while awaiting input. A frame-identity stall detector
  must additionally require the command bar to be *absent*.
* Actions: `Attack`, `Dodge`, `Charge` (restores CP), `Run`, plus **8 skill slots**
  (4 left + 4 right) = `S1`..`S8` in the reference config's vocabulary.
* Skills cost ~100 CP, several are **multi-target**, and they kill in ~2 hits vs 4+
  for `Attack`. A skill-led rotation is strictly better. CP regenerates between
  encounters.
* Skill/attack usage is **two-step: click action, then click the target.**
* Multi-enemy encounters are common (saw 3 and 4). Each enemy has its own name plate
  and HP bar at a **different y** — scan vertically, do not assume one bar position.
* Status effects render as **named red text with a stack count** (e.g. `Blood Feed (1)`).
* Reference constants from a different private server, treat as hints: 30s turn
  timer, 50-round cap. There is no reflex pressure anywhere in this game.
* **Do not click on a fixed schedule** — clicks issued during the enemy's turn are
  silently discarded. Detect, then act.

## Mission flow

```
Mission Room -> grade (S locked / A / B / C)
  -> paginated list (3 per page; Grade A = 7 pages, Grade C = 11)
  -> detail panel [Completed: N] -> green check
  -> cutscene ("click anywhere to continue")
  -> traversal (click to walk; encounters trigger on movement)
  -> N battles (Victory! panel each, XP 0 / Gold 0 — this is NORMAL mid-mission)
  -> epilogue cutscene
  -> "Mission Success!" (real rewards land here)
```

* Grade A spans Lv 42-78 in +2 steps; missions above the character's level show
  **greyed text + a padlock** and are inert. A locked-state detector is required or
  the bot will click a dead row forever.
* Story missions are **not stamina-gated** (flame column reads `-` throughout).
* Battle count is NOT the node count on the traversal track. "The Criminal
  Gathering" (Lv 56, Grade A) took **7 battles** despite showing 3 nodes.
* Grade choice is the biggest farming lever: Grade C page 1 gives 20 XP; Grade A
  page 1 gives 4,870.

## Other states

* `LOGGED_OUT` (`/` root, nav HOME/RANKING/DOWNLOAD/ACCOUNT, PLAY NOW!) and
  `LOGIN_FORM` (`/account`, username+password) are distinct states. On either:
  **halt and notify. Never attempt to authenticate.**
* Login queues **four** popups: Daily Login Reward -> Calendar -> Wishing Tree ->
  Lucky Spin. Dismiss controls are NOT uniform — small X (~59px disc), large X
  (~136px), and a back-arrow. Needs a drain-loop over a template set.
* The browser profile is the credential store — the bot never handles a password.
  Server-side expiry should surface to the human, not be auto-recovered, since it
  may mean a password change or ban.
* **CORRECTION — the session does NOT survive quitting the browser.** This entry
  used to say sessions persist in the profile. Measured: after `Browser.close`
  and a relaunch on the same profile, the page came back on the logged-out
  landing with only `_ga` and `cf_clearance` left, so the site's session cookie
  is a **browser-session cookie**. Consequences:
    - never restart Chrome to "get a clean window" — attach to the running one
      (`browser.launch` already reuses a live CDP port; `app.py --attach` forces it)
    - a crash or a quit costs a manual sign-in, and only the human can do it
* **Signing out is an explicit HALT, not an unknown screen.** `tpl/logged_out.png`
  (the "Welcome, Shinobi!" heading, 1.000 positive / 0.435 worst in-game) is the
  FIRST rung of the resume ladder. Before it existed the ladder still refused to
  authenticate, but only by exhausting `max_unknown` and reporting "unrecognised
  screen" — true, and useless to the operator.
* Cold SWF load ~25-30s; warm ~8s. Loading-state timeouts must span that range.
* Known stalls on this server: the Hunting House sub-app hangs at "Loading… 3%".
* The game console is noisy and prints `Out :: Error :: Main :: initButton` lines
  that are **not errors** — a log-scraping health check would false-alarm.

## The bot window — Chrome without browser chrome, panel inside the page

`engine/app.py` is the entry point: one command opens a window with the game on
the left and the controls on the right, like the reference bot's UI.

**There is no such thing as "embed the game in a native app".** Ruffle is
WASM + WebGL, the session cookie lives in this Chrome profile, and CDP is how we
click. Any native shell would have to host a browser engine and then expose a
debugging protocol to drive it — which is exactly what the reference bot IS
(Adobe AIR + CEF, i.e. a Chromium in a native frame). So the honest version of a
"native window" is **a browser window with the browser chrome hidden**:

    browser.launch(..., app_mode=True)   ->   --app=<url>

Measured: window chrome drops from **274 px to 90 px** — tab strip, omnibox and
bookmarks bar gone, leaving the title bar.

**The panel goes in the page's own gutter — the player element is never touched.**
CLAUDE.md's hard rule is that resizing `ruffle-player` via CSS desyncs
click -> stage mapping. It does not need resizing. Measured at the pinned
1720x720 viewport:

    game iframe (emulator.html)   x=375  w=960   -> right edge 1335
    free right gutter             1335..1720     -> 385 CSS px  (770 device px)

`engine/dock.py` puts a 380 px `position:fixed` panel there as a SIBLING of the
game, and `install()` asserts `overlaps: false` live rather than assuming it.

**The dock is invisible to perception.** Verified by scoring all 51 templates on
the same screen with and without it: 4 templates moved, all by <= 0.037, all
still at 0.16-0.31 against thresholds of 0.88, and **none matched inside the
dock**. There is no streaming anywhere in the viewing path — the operator is
looking at the real canvas at its own framerate.

### Getting a button press back to Python

    dock button -> window.__nsbot_send(json)   (Runtime.addBinding)
                -> Runtime.bindingCalled       (the CDP socket we already hold)
                -> cdp.drain_events()
                -> app.Runner._apply()

No HTTP server, no port, nothing polling the DOM. Three things had to be fixed
first, and each failed silently:

* **`CDP.call` DISCARDED every message that was not its own reply**, so events
  did not exist as far as this codebase was concerned. Nothing errored; they were
  simply gone. Events are buffered now (`_stash` / `drain_events`).
* **Waiting for events with a socket timeout corrupts the connection.** A timeout
  can expire in the MIDDLE of a websocket frame, after the header is consumed and
  before the payload, leaving the stream permanently desynchronised: every later
  read is garbage and the next `call()` blocks forever with no error. Wait with
  `select` — ask whether a read would block, then read the frame to completion.
* **Buffering every event is not viable on this game.** Enabling Runtime filled a
  512-slot buffer with `Runtime.consoleAPICalled` in under a second (the noisy
  console already recorded above), which would evict the button press. `cdp.watch()`
  is an allowlist; the dock watches only `Runtime.bindingCalled`.

### Two safety rails the dock needs

* **The panel is a NO-CLICK ZONE for the bot.** It is injected into the game page,
  so its buttons are as clickable as anything else on screen, and a stray bot
  click would press Stop or switch the task. `Actor.no_click_zones` refuses and
  logs. Every measured target is inside the game rect so this should never fire —
  which is precisely what was believed about the fixed card grid right before it
  clicked into the weapon Shop.
* **Pause must not deadlock.** The dock's Pause writes to `run/bot.control`, a
  long task parks in `Controls.wait_if_paused`, and the only loop that could read
  the operator's next button press is the one now parked. That hung live.
  `Controls.on_wait` is pumped every poll, so Run and Stop stay live mid-mission.

### A RELOAD LEAVES THE PANEL A SKELETON — the heartbeat must notice

When the page reloads, the BROWSER re-runs the injected bootstrap on the new
document by itself, and that first render carries **no state**: the Task buttons
and every value come up blank. Python is not involved in that re-injection and
never learns it happened. During a long task only `beat()` runs — which updates
the liveness clock but not the content — so the panel sat **empty but alive**
(no staleness banner) for the rest of the mission, and focus mode stayed off
because `ensure_focus` only runs BETWEEN cycles.

Both reported symptoms — "the task bar is broken again" and "not in focus mode
after relog" — were this one cause.

So the heartbeat reports whether the panel still has CONTENT, not merely that we
are alive: `"empty"` triggers a full render and a refocus immediately, rather
than at the end of a task that may be minutes away. `relog()` does both
explicitly too, so that path does not wait on the next beat.

**Do not answer this by re-rendering every beat.** A full render is a large
payload and the beat runs several times a second; the whole point of the
heartbeat is that it is one assignment. The test pins that a HEALTHY panel is
never re-rendered by the beat, and that focus is not forced back on when the
operator deliberately turned it off.

### The panel's "no bot attached" banner needs a HEARTBEAT, hung off CAPTURE

The panel decides it has been abandoned from the age of its last update, and a
full `render` only happens BETWEEN cycles — while a mission blocks for minutes.
So the banner claimed "no bot attached — the panel is frozen" for most of every
mission, with the bot working perfectly. A false alarm on a status light is
worse than no status light: it trains the operator to ignore it.

Two things were needed, and the FIRST FIX WAS NOT ENOUGH:

1. `Controls.wait_if_paused` now pumps `on_wait` even when NOT paused. It is
   called from the gate's poll loop, so it is the regular chance to service
   operator input; pumping only while paused meant a running mission never did.
2. **But the farm's own list navigation never enters a gate**, so the panel
   still went stale for the whole of pagination. The hook that actually covers
   everything is `Capture.on_activity`, called from `frame()` — *every* part of
   this bot looks at the screen constantly: the resume ladder, farm navigation,
   gates, missions, minigames.

`Runner.beat()` is throttled to 3 s because captures run many times a second and
each beat is a CDP round trip. Measured after the fix: worst staleness **3.2 s**
against a 12 s window, across pagination and combat.

Re-install `cap.on_activity` after a reconnect — the Capture object is new, and
forgetting leaves the panel permanently stale from that point.

### Switching task must INTERRUPT the running one, and relog if it is stuck

Two faults made the Task buttons feel dead, and together they pushed the
operator into using Stop — which now kills the process, so the panel detached.

**1. The operator's buttons were not being READ during long work.** `pump` ran
only from the gate's poll loop, and farm navigation — paging the mission list,
opening a mission — never enters a gate. So a press could sit undelivered for
the whole of it. Measured: the command was sent and **no `task` event ever
reached the log**. `Capture.on_activity` now pumps on every capture, which is
the one hook every code path passes through; draining is cheap (a `select` on a
socket with nothing on it), and the heartbeat inside `pump` keeps its own
throttle. Re-entrancy is guarded, because `_apply` can itself capture.

**2. Setting the task did not stop the task already running.** A mission takes
minutes and the cycle loop runs it to completion, so the new choice only took
effect a whole mission later. The switch now throws the file-backed stop switch
every task already honours at its gates, the old task unwinds cleanly, and the
loop re-arms it — **in a `finally`**, because if `step` raises on the way out,
a thrown switch would wedge the bot in a stop no button explains.

**And a task that needs the LOBBY cannot start from inside a mission at all.**
The ladder deliberately cannot name a battle or traversal screen, so switching
to TP training mid-farm just accumulated unrecognised frames until the bot
paused. After `relog_after_unknown` (8) unreadable frames it now RELOADS:
character select is a screen the ladder knows, and it walks back to the lobby
from there. Verified live, the whole chain:

    task -> tp_training (interrupting farm_missions)
    switched to tp_training
    state unreadable for 8 frames - relogging (1/2)
    resume: select_char -> play (0.979 BY TEMPLATE)
    resume: lobby (1.000) -> Mission Room (1.000) -> Special tab (1.000)

It is deliberately not the first response — a relog throws away an in-flight
mission — and it is capped at `max_relogs` (2) per streak so a screen that
survives a reload cannot loop forever. The budget resets whenever the ladder
recognises something, so it is available for the next, different problem.

The relog is a RELOAD AND NOTHING MORE. It never authenticates; the ladder
clicks Play **by template** at character select, so `Delete` beside it is never
a candidate.

### Stop aborts the TASK; Quit exits the process

A cooperative stop does not work here, and the reason is structural: `_apply`
is reached from `pump`, which is called from the capture hook and the gate's
poll loop — and **both wrap the call in `except Exception`**, so an exception
raised to unwind the stack is silently swallowed. The flag-and-check
alternative only takes effect wherever something next bothers to look, which
mid-mission can be a whole battle away. Pressing Stop and watching the bot
finish the fight is not what Stop means.

**CORRECTION — the cause was not the mechanism, it was that nobody was READING
the button.** `pump` ran only from the gate's poll loop, and farm navigation
never enters a gate, so a Stop could sit undelivered for the whole of it. Once
`Capture.on_activity` pumped on every capture, the file-backed stop switch is
seen within a capture or two and the task unwinds at its next gate.

Stop briefly called `os._exit` to get that immediacy. That was the wrong trade:
the panel is injected into the PAGE so it survives the process, but its buttons
then have no receiver — the operator is left with a dead panel reading "no bot
attached" and no way back except the terminal. Which is exactly what they kept
hitting. **Stop now aborts the task and keeps the process; Quit exits.**

`os._exit` skips `finally`, so **`_hard_exit` must release the pid lock
itself** — forgetting that is precisely what produces "another bot window is
already running" on the next launch. It deletes the lock only when the file
holds OUR pid, never another process's.

The two buttons now differ only in what they leave behind:

    Stop -> the task stops; process and panel stay live, Run resumes
    Quit -> the panel is removed and the process exits

### A BARE PID IS NOT A LOCK — pids get reused

`os.kill(pid, 0)` only proves SOMETHING is alive, not that it is us. Observed:
the lock held a pid from a long-dead instance, the OS had recycled that number
for an unrelated process, and a launch was refused with *"another bot window is
already running"* **while no bot was running at all**. The operator's only
recourse was to delete `run/app.lock` — which is exactly the habit that let
eight instances stack up in the first place. A guard that pushes you toward the
thing it exists to prevent is worse than no guard.

The lock now records WHO: `{"pid": ..., "cmd": ...}`, and a claimant must match
on IDENTITY (the live command line still equals the saved one). A lock failing
that is stale and is removed on sight, so a launch is never blocked by a
stranger.

**Identity is the primary test, not a name marker.** The first attempt required
`"app.py"` in the holder's command line, and that DROPPED A LEGITIMATELY HELD
LOCK whose command did not contain the marker — a false negative that would let
two instances run. The marker survives only as the fallback for a legacy
bare-pid lock that recorded no command.

Five cases, all verified: recycled pid -> stale; dead pid -> stale; genuine
holder -> respected; same pid running a different program -> stale; legacy
bare-pid naming a stranger -> refused.

### Closing the window must CLOSE THE BOT

A closed window and a navigated page look identical at the socket — both simply
kill the connection — but they need opposite responses: reconnect to a
navigation, exit on a closed window. Without separating them, `reconnect` ran 30
`attach` attempts at up to ~32 s each **while holding the pid lock**, so closing
the window and relaunching produced *"another bot window is already running"*.
Measured: one such process was still alive, and wedged, **seven hours** later.

`Runner.browser_alive()` separates them with one local HTTP request to the CDP
endpoint, which dies with the browser process. Two places use it:

* `reconnect` — three consecutive failures means the window is gone: set `quit`
  and return, never paying for an attach. Measured 4 s instead of up to 16 min.
* the main loop — polled every few ticks, because **the window can be closed
  while the bot is idle** and then nothing raises at all: no call is in flight,
  so no socket error ever surfaces and the process just sits there holding the
  lock.

The trailing `push()` after the loop is wrapped, since it cannot succeed when
the browser is what died, and the caller's `finally` is what releases the lock.

Note the lock itself was never wrong: it checks `os.kill(pid, 0)` and so ignores
a stale pid. Deleting `run/app.lock` to "fix" a refusal is the wrong move — it
defeats the guard and lets duplicate instances click the same game (six were
found running at once). Kill the process instead.

## Safety rules (non-negotiable)

* **Never enter credentials.** Not from the user, not from storage, not "for testing".
* **Never spend tokens.** The premium currency. Known token sinks: Mission Room NPC
  recruit `+` buttons (T20/T40/T60), `+` buttons beside the gold/token HUD counters.
* **Once-per-day resources need explicit per-use consent:** `Claim` (daily reward),
  `Wish` (Wishing Tree), `SPIN` (Lucky Spin).
* **`Delete` sits next to `Play`** on character select. Whitelist `Play` by template;
  never click by offset.
* Logging must redact URLs and console output — both can carry the session token.

## Combat — mission #2 additions (measured)

* **Enemies regenerate.** Observed enemy HP: 50.7 -> 43.0 -> 43.0 -> 43.0 -> **47.2**.
  It went back UP. A weak-damage loop can be fully cancelled by enemy regen,
  producing an unwinnable fight with no error and no end condition.
  `engine/combat.DamageWatchdog` exists for exactly this - it aborts (take `Run`)
  when no new low is reached for N turns. Do not remove it.
* **Regen also fires MID-combat**, not only between encounters (`+250 HP` seen
  during a fight). Any HP-threshold logic must tolerate HP going up.
* **`Attack` is weak: ~8 percentage points per hit.** Skills are far better when
  they land. Six Attack cycles looked like zero progress and were not.
* **Skill slots are TYPED, not uniform.** Right-bank slot 1 applied
  `Strengthen(1)` to self for 50 CP - a buff, no damage. Slot CP costs vary
  wildly (~10 CP for one pair, ~100 CP for another). The config must declare
  each slot's type and cost; never assume a slot deals damage.
* **Cooldown detection cannot use a global saturation threshold.** Measured mean
  saturation across the 8 slots in one frame: 56.2 .. 190.8, CONTINUOUS with no
  bimodal split. The pale-pink slot reads 56 while perfectly usable. Use
  `engine/combat.SlotBaseline`, which compares each slot to its own ready-state
  sample.
* **RESOLVED — the 8-slot centre ring is a TURN-SCOPED JUTSU CAST PANEL, not a
  target selector.** This entry was wrong twice before settling; the evidence is
  recorded here so it does not get re-litigated.

  What was claimed and why it was wrong:
  1. First claimed "transient" — from probing one geometry's coordinates against
     another's frames. Wrong method.
  2. Then claimed "persistent, and almost certainly the target surface" — from
     the reference bot's fixed `T1..T8` battlefield slots. Wrong semantics.

  **What a live click actually did** (`ref/auto/mission/ring_before.png` ->
  `ring_after.png`): clicking a filled slot **consumed the turn** (command bar
  present -> absent, 12.7% of pixels changed) and cast **`Strengthen`** — the
  buff appeared as red floating text over our own character. The ring then
  **disappeared**.

  So the model, which fits every frame we hold:

  | frames | command bar | ring |
  |---|---|---|
  | t0-t3, boss_t0-t4 | present | present |
  | epi_* (cutscene)  | absent  | absent |
  | ring_before       | present | present |
  | ring_after (acted)| absent  | absent |

  The ring is co-present with the command bar: it is drawn while awaiting your
  action and vanishes once you act. Filled slot with a coloured border =
  castable jutsu; grey = empty slot. It is effectively a **second action bar**,
  functionally like `S1..S8`.

  Consequences for the bot:
  - These are ACTIONS, not targets. They belong in `battle.rotation`, never in a
    target step. `battle.click_target` stays **false**.
  - They are TYPED like the skill slots — the one measured cast a self-buff for
    no damage. Declare each in `battle.slot_kinds` before use.
  - `engine/geometry.py` locates all eight correctly; only the name `TARGETS` is
    a misnomer, kept for now to avoid churn. Read it as "ring action slots".
  - Their presence is a usable **"it is your turn"** corroborator alongside the
    command bar.

  **Targeting is therefore still open.** The only mechanism with positive
  evidence remains `Attack` + enemy NAME PLATE (-7.7pp verified). Also measured
  live: `Attack` alone **resolved the turn with no separate target click**, so
  the two-step action->target model is not required for `Attack` here.

* **`find_enemy_bars(y0=0, …)` returns the PLAYER HUD as enemy bars.** Measured
  live: 11 "bars" found, of which four (y=39, 59, 86, 101) all read exactly
  100.0% — those are the top HUD (own HP/CP, gold/token fill), not enemies.
  Two consequences, the second serious:

  1. `DamageWatchdog` fed from these gets garbage and could abort a winnable
     fight or miss a stalled one.
  2. **A bar-derived click can land in the HUD row that holds the token `+`
     sinks.** In this run the click went to (1176, 39), ~330px clear of the gold
     `+`, and the token count was verified unchanged at 538 — but the class of
     bug is a token-spend risk and must be fixed before any bar-derived click is
     armed. Constrain the scan to the battlefield band and validate a candidate
     before clicking it.

* **Battle geometry must be ANCHOR-RELATIVE, never absolute.** The reference bot
  hardcodes ~2,500 absolute coordinates; it can, because it forces one window
  size. We cannot — our own capture sets differ by 18% (command bar at scale
  0.46 in one, 0.545 in the other). An early probe of mine reported the target
  ring as "transient" purely because it tested one geometry's coordinates
  against the other's frames. It is not transient; the coordinates were wrong.

  What holds instead: locate the command bar (`charge_btn` + `dodge_btn`, which
  also yields the scale), then compute every slot as
  `anchor + offset_in_template_units * scale`. Verified by deriving offsets from
  the 0.46 frames and predicting the 0.545 frames — **8/8 ring slots landed on
  their real borders**, and skill slots within ~6px of independently measured
  ones. The two geometries are a pure uniform scale: command-bar pitch / match
  scale was 108.7 vs 108.3, agreeing to 0.4%.

* **The command bar is a 2x2 block, not a row.** Attack top-left, Dodge
  top-right, Charge bottom-left, Run bottom-right; side 108.7 template units
  (50px at scale 0.46, 59px at 0.545).

* **`min_conf` for the command bar is 0.70, not 0.85.** The discrimination matrix
  below was measured on ONE geometry. On the 0.545 capture set the same real
  command bar only reaches 0.746/0.788, because matchTemplate is not scale
  invariant — an 0.85 gate silently classified every boss-encounter frame as
  "not combat". Measured separation: command bar present 0.746..0.949, absent
  0.407..0.470. Gate at 0.70 and let geometry cross-check the rest.
* **Status effects** seen: `Blood Feed (1)`, `Strengthen(1)`, `Blind(1)` - named
  red text with a stack count. Damage numbers render as large floating white text.
* **Result panels are dismissed by their GREEN CHECK, not by clicking anywhere.**
  Measured live: a mid-mission Victory panel absorbed **eleven** clicks at the
  canvas centre and did nothing. The panel body is not a hit area; the green
  check bottom-right is the only one. Clicking the template-match centre (the
  banner) fails the same way.

  The check is the **same glyph** the mission detail panel uses to START a
  mission (`tpl/mission_start.png` serves both). That is exactly why `classify()`
  must test the result panels BEFORE `mission_start` — otherwise a Victory panel
  reads as "start a mission".

  **It is drawn at THREE DIFFERENT SIZES, and that is the trap.** Measured peaks
  of the same glyph:

  | where | scale | conf |
  |---|---|---|
  | mission detail panel | **1.00** | 0.975 |
  | mid-mission Victory  | **1.18** | 0.974 |
  | Mission Success      | **1.84** | 0.972 |

  All ~0.97 at their true scale, so this is a pure SCALE problem, not a quality
  one. A narrow 0.90..1.15 sweep caught Victory only at its edge and missed
  Mission Success entirely (0.693), so the runner refused to click — correctly —
  and the mission could never close out. The sweep must span **0.95..1.95**.

* **A mission is not finished when "Mission Success!" appears — only once its
  green check is acknowledged and the game is back in the lobby.** Verified live:
  click the check -> panel clears in 0.34s -> lobby anchor returns in 0.33s.

  Returning SUCCESS on sight of the panel was a false-success bug with a nasty
  second-order effect: with `--repeat N` the next runner started while the panel
  was still open, re-classified `mission_success`, and banked another instant
  success — N missions from one panel, never once returning to the lobby to start
  a real one. `MissionRunner` now requires the acknowledge AND the lobby before
  reporting SUCCESS, and records `stats["closed_out"]`.

* **Mission Success vs mid-mission Victory — measured, and cleanly separable.**
  Confirmed on "Blacksmith's Trouble": the mid-mission Victory panel showed
  **XP 0 / Gold 0**, while Mission Success showed **XP 11,630 / Gold 2,200**.
  So only Mission Success may increment a success counter, exactly as recorded.
  Template cross-check (both directions, so the counters cannot lie):

  | template | Victory frame | Success frame |
  |---|---|---|
  | `result_panel`    | **1.000** | 0.328 |
  | `mission_success` | 0.407     | **1.000** |

* **Mission flow varies between missions.** #1: cutscene -> traversal -> combat.
  #2: cutscene -> **loading** -> combat (no traversal), then traversal later.
  Branch on observed state; never follow a fixed script.
* The `Loading...` interstitial resolves normally (0% -> done). The Hunting House
  hang at 3% was specific to that sub-app, not a general loading defect.

## Navigation — two measured faults behind "clunky" farming

Both found by watching a live farm run stall on Grade A page 5/7, and both
reproduced offline against `ref/auto/mission/list_all_locked.png`.

**1. A missing command bar cost a FULL SWEEP on every cycle.**
`BattleGeometry.locate` already budgeted its misses on the *hint* path, but the
*cold* path — no hint cached — fell straight through to the 90-scale sweep.
Measured **12.9 s** on a 3440x1440 frame. `farm.in_mission` calls it once per
cycle, so a process that had never seen a battle paid 12.9 s per cycle to be
told there is no command bar, forever. Symptoms: the dock froze for 40 s at a
time, `uptime` stopped advancing, and an operator Stop was not read until the
sweep finished — it looked like a hang, and was 100% CPU in `matchTemplate`.

Fixed by giving the cold path the same `REACQUIRE_AFTER` budget. The FIRST cold
miss still pays in full, so an unfamiliar geometry is still discovered; after
that the narrow sweep carries it. Measured **12.94 s -> 0.96 s**, and combat is
untouched because it runs on the hint path (~62 ms).

**2. A mission LIST page was classified as walkable scenery.**
`looks_like_mission_scene` is a negative definition — it returns True when none
of the "not in a mission" anchors match — and on Grade A page 5/7 *none of the
six matched*: `grade_tab` 0.506, `mission_room` 0.417, the rest 0.28..0.51. So
the runner "walked" by clicking the map edge INSIDE the mission list, the
mission never started, and it never left the page.

The list page does carry high-margin anchors; they simply were not in the set:

| template | list pages | everything else |
|---|---|---|
| `page_next` | 0.973 .. 1.000 | 0.445 .. 0.600 |
| `page_prev` | 0.973 .. 1.000 | 0.496 .. 0.600 |
| `mission_locked` | 0.946 .. 1.000 | 0.381 .. 0.402 |
| `list_back_arrow` | 0.960 .. 1.000 | 0.417 .. 0.467 |

All four separate by >0.37. `list_back_arrow` (newly cut) is the broadest — it
is on the list AND the detail panel — which makes it the reliable "this is list
UI, not scenery" signal.

**Corollary: `to_grade_panel` could not back out of a list.** It handled lobby,
Mission Room and grade panel, but from a list or detail page `mission_room_entry`
does not match and the `story_tab` branch was dead code — **there is no
`story_tab` template**. It now presses `list_back_arrow` up to twice
(detail -> list -> grades), verifying after each.

**A negative definition needs a positive veto for every UI surface it can meet.**
That is the general lesson: "no anchor matched" is not evidence of scenery, it is
evidence that the anchor set is incomplete — and the action it licensed here was
blind clicking.

### Do not take CLIPPED screenshots from a second CDP client while the bot runs

`Page.captureScreenshot` with a `clip` applies its own device-metrics override
and restores it afterwards. A second client polling clipped frames therefore
RESIZES THE PAGE repeatedly, fighting the viewport `browser.pin_viewport` pins.

Observed while recording a farm run for debugging: the canvas and the dock moved
under the bot, and clicks aimed at the game landed on the panel instead —
setting a mission pin, toggling focus mode twice, and pressing **Relog**, which
reloaded the page and dropped the session to character select. The log shows all
of them as `operator:` events; no operator issued any of them.

Two lasting consequences:

* Record with FULL-frame captures, or drive recording from the bot's own client.
* The no-click zone must be re-read every cycle, not captured once at attach —
  a guard that defends where the panel *used to be* is worse than none, because
  it reads as protection. `Runner._refresh_no_click_zone` does this now.

The safety rule that DID hold: the resume ladder reached character select and
clicked **Play by template** (`play_btn` 0.979), never by offset, so `Delete` —
which sits beside it — was never a candidate. That is exactly why the rule is
"whitelist Play by template".

### LEAD (not wired up): the game draws its own "Go!" direction arrow

On the first map of "Desert Ronins" the game drew an orange **"Go!" badge with a
right-pointing arrow** at (2542, 380). If that arrow is drawn on every traversal
map and mirrors for leftward ones, it would replace the heading coin-flip
outright — it is the game telling us which way to walk.

**It is NOT wired up, because one sighting is not evidence.** Scored the cut
arrowhead across every traversal frame held:

| frame | conf |
|---|---|
| the map it was seen on | **1.000** |
| six other traversal frames | 0.293 .. 0.331 |
| lobby / mission room / combat | 0.279 .. 0.421 |

So it is absent from every other traversal screen captured. It may be an
entry-screen hint rather than a per-map indicator — or those six frames may be
unrepresentative, since they came from the render-stalled mission where the
character was never drawn either.

The crop is kept as `tpl/_lead_go_arrow.png` (leading underscore, so
`load_templates` skips it) purely so it does not have to be re-cut. **Before
using it:** confirm it appears on several NORMAL traversal maps, and establish
what a leftward map draws — a mirrored arrow, a badge on the other side, or
nothing. Until then traversal keeps alternating, which costs one wasted run on a
wrong first guess and is honest about not knowing.

### Traversal heading comes from the CHARACTER, found by saturation not hue

The operator's "it cannot find the target from the next area" was a heading
coin-flip. `kekkai_play.find_character` keys on a RED robe; at Lv 65 this
character wears purple, so it returned None and mission traversal ALTERNATED
instead. Combined with `_scene_changed` reporting "moved on" when the character
merely walks WITHIN a map, that oscillates:

    right -> dead end -> left -> moved on -> left -> dead end -> right -> ...
    13 traversal runs, 5 dead ends, 0 encounters

**Hue is the wrong invariant - gear changes.** Saturation is not: a player
sprite is far more saturated than the painted scenery, and is small and TALL.
Measured with the map band isolated (below the HUD, above the NPC rail):

| | saturation / shape |
|---|---|
| desert sand | median 111, p90 **126** |
| character (purple robe) | area 1245, bbox 62x107, at (2431, 587) |
| character (same, other map) | area 1291, bbox 62x106, at (911, 487) |

A gate at **150** leaves exactly ONE blob on a frame with the character, and
ZERO on the lobby, on combat, and on six frames from the render-stalled mission
where the game never drew it. `MissionRunner.find_character` does this.

Then the spawn rule this file already records for Kekkai applies: you enter a
map through one edge, so head AWAY from it — `x < centre -> right`, else left.

**Independently corroborated:** on the entry map the detector put the character
at x=911 and said "head right", and the game itself drew a right-pointing
**"Go!" arrow** on that very frame.

**Also: click the CHARACTER'S OWN ROW, not a fixed ground line.** `GROUND_Y` is
880, which on the desert map is ~240 px BELOW the character's feet — off the
walkable path, so the run barely moved and then read as a dead end. That is half
of why the oscillation never resolved.

Measured live, same stuck mission, before and after:

| | runs | dead ends | encounters |
|---|---|---|---|
| alternating heading, fixed GROUND_Y | 13 | 5 | 0 |
| character-derived heading and row | **2** | **0** | **1** |

### COOLDOWN IS NOT RESTRICTION — always try Attack before Dodge

`SkillRotation.candidates()` appends the fallback (`AT`) **LAST**, so the stun
short-circuit's early `break` jumped clean past Attack straight to Dodge. With
skills merely on COOLDOWN that is the wrong action entirely: the bot spent its
turn dodging while Attack was available and would have dealt damage. Reported
as "skills being spammed on cooldown but never the auto attack".

**The two cases are distinguishable, and the game distinguishes them for us:**

    a COOLDOWN disables only that skill      -> Attack still resolves
    a STUN disables everything except Dodge  -> Attack fails too

So stop probing skills after `restricted_after` misses, but NEVER skip the
fallback: try Attack, and fall to `restricted_action` only when that fails as
well. Dodging is then a measured conclusion rather than a guess.

**A test that passed for the wrong reason.** The original restriction test built
its rotation with no fallback at all, so `AT` was never in its candidate list -
it had been asserting against a configuration the bot never runs
(`battle.fallback` defaults to `"AT"`). It only passed because the old `break`
never needed to reach the fallback. Both tests now assert the ORDER: Attack
strictly before Dodge.

### A stunned turn must not probe the whole rotation

Stun greys out every action except Dodge, and clicking a disabled button does
nothing at all — so each rotation candidate burns a full resolve timeout (~6 s)
to establish what the previous one already did. Measured live on "Desert
Ronins": S4, S5 and S1 each timed out inside ONE round, about **24 s** to reach
a Dodge that was the only legal move the whole time.

One failure is ambiguous — a cooldown, or a click that missed. **Two consecutive
failures in the same turn is the stun signature**, so `_take_action` stops there
and takes `restricted_action` (default `DO`). Nothing is permanently given up:
if the restriction was real, Dodge resolves at once; if it was not, the next
turn starts the rotation again from the top. `battle.restricted_after` tunes it.

Note this is the *cheap* direction of the trade. Probing more costs 6 s a go and
tells us nothing new; probing less costs at most one turn spent dodging.

### THE COMMAND TEMPLATES CARRIED THE MAP BEHIND THEM — re-cut tight

A farm mission stalled on a NIGHT map. The bot was plainly in combat — Attack,
Dodge, Charge, Run on screen, three enemies, the ring up — and ran TRAVERSAL,
clicking map edges. The buttons were found at exactly the right places but too
faintly to gate on:

    charge 0.613   dodge 0.725   attack 0.700   run 0.507      (gate 0.70)

**Not a scale problem** — the best score was at scale 1.0, the same scale that
reaches 0.867/0.986 on a daylight frame. The templates were cut **110x86**, wide
enough to include the map background around each disc, and a dark map destroys
that correlation. Re-cut to **78x78**, the disc only:

| | dark map | daylight | worst non-combat |
|---|---|---|---|
| old wide cut | 0.613 | 0.867 | — |
| new tight cut | **1.000** | **0.949** | **0.431** |

The old crops are kept as `tpl/_wide_*.png` (underscore = not loaded).

**RE-CUTTING A TEMPLATE MOVES THE GEOMETRY ANCHOR.** `BattleGeometry` derives
every offset from the charge/dodge match centre, and the wide crop included the
label BELOW each disc, putting its centre ~25 template px above the disc centre.
`CMD_ANCHOR_DY = 25` restores the historical anchor rather than re-deriving
~2,500 measurements. Three mistakes on the way, all caught by the suite and all
worth remembering:

1. **a raw 25 px** — right at scale 1.0, wrong by 12 px at 0.46, because
   25 * 0.46 = 11.5. The offset is in TEMPLATE UNITS and must scale.
2. **only one of three code paths** — `locate` has hint / narrow / full-sweep
   branches and the first two return early, so correcting the full sweep alone
   missed the common case. It belongs in `_best`, which all three share.
3. **float centres** — the scaled subtraction produced floats, and those centres
   are used to SLICE frames. Round to int.

`action_flag` cannot be fixed the same way: on that frame an enemy sprite
OCCLUDES the "Action!" text (0.750). But the two are complementary — the flag
carries the between-turns frame (0.897) where no buttons are drawn, the buttons
carry the dark frame (1.000) where the flag is occluded — so both are in
`NOT_IN_MISSION` and between them every combat state is vetoed.

### A battle BETWEEN TURNS has no command bar — gate combat on `action_flag`

This is the third instance of "a negative definition needs a positive veto",
and the most damaging one, because what it licensed was walking during a fight.

Between turns the game draws no command bar at all. Measured on a live frame
with three Lv64 enemies on screen, name plates and HP bars drawn, and the turn
marker still travelling:

| template | between turns | with the bar |
|---|---|---|
| `charge_btn` | 0.371 | 0.867 |
| `dodge_btn` | 0.328 | 0.986 |
| `attack_btn` | 0.307 | 0.989 |
| **`action_flag`** | **0.897** | **0.993** |

`action_flag` reads 0.223..0.255 on traversal, the lobby and the mission room,
so it separates by 0.64 and is the ONLY anchor that survives the between-turns
gap. It is now in both `IN_MISSION` and `NOT_IN_MISSION`.

Before that, `in_mission` returned None mid-battle, the ladder called the screen
unknown, and after three unknowns the runner walked — clicking the map edge
while three enemies waited. From outside that looks precisely like the bot
"skipping the enemy".

### Saturated SCENERY beats the character on AREA — select by HEIGHT

The saturation finder above must not pick the biggest blob. A yellow-green shrub
on a rock at the map edge measured **48x77, area 2534**, beating the real
character's **79x123, area 1585**. So the bot "found" its character at the same
pixel (779, 917) every single run, always concluded "head right" because that x
is left of centre, ran into the edge it was already standing on, and logged
**8 dead ends** while an enemy stood in plain sight.

Measured character heights are **106, 107, 123** across three maps against the
shrub's 77, so height separates cleanly where area inverts the answer: a bush is
short and broad, a ninja is tall and narrow. `CHAR_MIN_H` is 95 and the TALLEST
qualifying blob wins.

### "DO NOTHING" MUST LEAVE EVIDENCE — save the frame that defeated us

Refusing to click on a screen the bot cannot name is the right ACTION, and it is
what stops a blind click. But on its own it teaches nobody anything, and the
screen is gone by the time an operator looks.

Every unrecognised screen in this project turned out to be ONE anchor from ONE
frame away from handled - the mission list, a battle between turns, the
seal-broken dialog, the Level Up panel. The hard part was always CATCHING the
frame. So on the `teach_at_unknown` (4th) unrecognised frame - before the relog
wipes the screen away - the runner writes it to `ref/auto/unknown/` and says
plainly that it needs teaching, with the recipe.

### CLICK A UNIT AT ITS FEET, NOT ITS TORSO

A walk-to click wants the GROUND the unit stands on. The moving blob's centre is
mid-sprite: measured 222x245 centred (2169, 990), so the sprite spans
y 867..1112. Aiming a third of the height below centre lands at 1070 - near the
feet, still firmly ON the unit, so the character walks TO IT rather than to open
ground beside it.

### THE MISSION PROGRESS TRACK — real, but not universal

Some maps draw a track along the bottom: a start icon, node dots, and a red
"Goal" marker. Measured by the width of its saturated yellow bar:

    desert map   1482 px at y=702   -> track present, 3 nodes
    dark map      450 px            -> no track

So it can answer "how many sections remain" where it is drawn, and cannot answer
"what kind of section is this". For the latter, the positive phase classifier is
the mechanism - see the note on negative definitions.

### MOVEMENT IS THE BEST ENEMY DETECTOR — enemies animate, scenery does not

Measured on a live traversal map with one enemy standing on it: six frames
0.35 s apart, differenced and thresholded, give exactly **ONE** blob —
area 19560, 222x245, centre **(2169, 990)** — against an enemy really at
~(2179, 991). Ten pixels.

On that same frame the colour/shape pass found **only canopy scenery**
(y 254..382) and returned **None** for the enemy, because it stands at y=991,
below `FIG_BAND`'s 950 floor.

Two reasons movement wins outright:

* it cannot be fooled by scenery, which is what the colour pass keeps proposing
  (a cactus, a bush, roof tiles);
* **our own character does not animate while idle**, so a moving blob needs no
  "that one is us" exclusion at all - the colour pass needs a 220 px window and
  still gets it wrong.

`MissionRunner.find_moving_figure` does this and runs FIRST in traversal, with
the colour pass as the fallback for a frame where nothing moved. It costs ~1 s
in captures against the 6.5 s timeouts it avoids.

**A WRONG CONCLUSION, KEPT BECAUSE THE MISTAKE IS THE LESSON.** This was first
measured on an EMPTY map - mean difference 0.00, zero blobs at any threshold -
and written off as "animation is not a signal here". That test could not have
worked: nothing alive was on screen. **Measure the thing you are trying to
detect.** The operator pushed back on the conclusion and was right.

(Contrast the hand-seal board, where the training dummy animates continuously
and differencing is useless for the opposite reason. Motion is a signal exactly
where the still parts are still.)

### A FIGURE CANDIDATE MUST BE ON WALKABLE GROUND

The runner "never moved to the enemy" because the candidates were in the TREE
CANOPY: `(1907, 254)` and `(2513, 261)`, 17% down the frame. Clicking there
cannot move the character at all, so the spot could never be reached - 6 s
timeout, marked scenery, repeat.

**What we LOOK AT and what we ACCEPT are different questions.** `FIG_BAND` stays
(200, 950) because the mask is built from the ROI's own background MEDIAN and
narrowing it changes that estimate and loses real enemies - that mistake already
broke enemy detection once. Acceptance is separate: `FIG_MIN_Y = 420`, below the
measured range of real enemies (y 460..875) and of our own character (487..805).

### NEVER RUN INTO AN EDGE YOU ARE ALREADY AGAINST

The heading comes from a pixel detector, and when it misfires the heading
INVERTS. Measured on a dark forest map: the character stood 64 px from the
canvas's left edge while the finder reported foliage on the right, so the runner
clicked the LEFT edge seven times with no progress.

The guard is geometric and needs no detector to be right: if the character is
already within a few strides of the edge it is being sent toward, go the other
way. Mid-map it does not interfere.

### REMEMBER A DUD WITH TOLERANCE — a centroid jitters, an exact tuple never matches

The "don't try that spot again" set matched on an exact `(x, y)`, and a blob
centroid moves a pixel or two between frames. So it never matched, and the bot
re-clicked the same scenery indefinitely. Measured in one session:

    509 failed engagements
    ONE piece of scenery retried SEVENTY times
    the same bush as (1199,706) (1200,706) (1200,707) (1201,705) (1201,707)
    at ~6.5 s per attempt

That is most of an hour spent clicking a bush, and from outside it looks like
"the bot is acting strange and nothing works". `DUD_RADIUS` is 40 px.

**Tolerance alone is not enough**: a map full of shrubs can still offer a FRESH
candidate every pass. After six failures on one map, stop proposing targets and
walk — the edge run is the reliable move.

General rule for any "remember what failed" set built from pixel measurements:
**compare with a radius, never with equality.**

### A DIAGNOSTIC NOTE, worth more than it looks

`engine/app.py` launched bare writes its log to the TERMINAL, and `run/app.log`
then holds a STALE log from a previous session - which was read as current and
nearly produced a wrong diagnosis. Check `ls -l run/app.log` against the clock
before trusting it, and prefer `> run/app.log 2>&1` so the log is real.

### A story map must be CLEARED before you leave it

`_traverse` only ever ran to the MAP EDGE. That rule came from the Kekkai
seal-hunt, where it is correct, and it is WRONG for story missions: the map has
to be cleared first, then you move on. The operator put it exactly right — "it
should run towards it and only when killed shall it walk to the next map".

Worse, running to the edge along OUR OWN ROW cannot even collide with the enemy.
Measured on one desert map: the enemy stood at **y=460** while our character was
at **y=864**, so the run passed 400 px beneath it. The mission skipped its first
fight and then wandered.

Figures — ours and theirs — are found by **colour distance from the map's own
background**, which adapts per map instead of assuming sand or grass:

| region | frac(distance > 60) |
|---|---|
| flat sand | 0.002 |
| enemy ninja | 0.150 |
| our character | 0.570 |

**The detector cannot fully separate a sprite from scenery, and that is
accepted rather than papered over.** A CACTUS at (1033, 748) is proposed as a
figure on two different frames. Things that do NOT work, both measured:

* skin tone — desert sand scores **0.951**, higher than the ninja's 0.841
* dark outlines — the enemy is low-contrast tan-on-tan and is missed entirely

So engagement is a GUESS THE GAME VERIFIES: click the candidate, wait on
`command_bar` / `action_flag` / a result panel; if no fight starts, the spot is
remembered in `_dud_targets` and never offered again, and the ordinary edge-run
happens. A wrong guess therefore costs exactly what a dead end already cost —
one cycle — and cannot loop.

**Still weak:** calibrated on ONE frame that contains an enemy. More
frames-with-enemies, across map types, are needed before the figure filter can
be tightened.

## The single biggest lesson

**Never judge a bar, or "no change", by eye. Measure it.**

Four wrong conclusions this session came from visual estimation, each corrected by
a calibrated measurement:

1. "Battle is frozen" - it was a normal turn-based wait.
2. "Enemy is taking no damage" - a loose mask reported every bar as 100%.
3. "Enemies are on slivers" - they measured 43-56%.
4. "Blind is causing misses" - damage was landing at ~8pp all along.

The same discipline applies to code: `DamageWatchdog` had two logic bugs that only
surfaced when replayed against the real measured sequence. Test guards against
recorded data, not intuition.

## From the game's own archived client (authoritative, not inferred)

Source: publicly archived decompiled Ninja Saga client, `battle/BattleProcessor.as`
and `DataParser.as`. This is the GAME's code, used purely as mechanics reference.

* **Cooldowns are counted in ROUNDS.** `nextRound()` calls
  `reduceSkillCooldown(1)`. So cooldowns are small integers and are exactly
  trackable by bookkeeping — record the round a slot was used, and it is ready
  `cd` rounds later. Use `engine/combat.CooldownTracker`.
  **Do not use icon saturation as the primary cooldown signal**; it was measured
  unusable as a global threshold. `SlotBaseline` is a cross-check only.
* **CORRECTION — a status effect's `(n)` is a DURATION IN ROUNDS, not a stack
  count.** Buffs/debuffs carry a `duration` decremented each round
  (`updateRoundBuff`/`updateRoundDebuff`) and are removed at zero. So `Blind(1)`
  meant one round remaining. Some effects transform on expiry (e.g. a gate
  effect becoming a stun).
* **Targets are addressed by character ID** (`setDefenderById()` scanning
  `characterArr` / `petArr`), not by sprite hit-testing at the battle layer. The
  click -> ID mapping lives in the UI layer above it, which is why clicking a
  name plate vs a sprite gave inconsistent results. This is also exactly why the
  reference bot exposes **`Auto` vs `ID`** target selection with a numeric field.
* **Turn model confirmed:** `characterTurn(type, id)` for player/enemy/party/pet,
  one action per cycle, player input routed via `processCommand()`. Gating on
  command-bar presence is a valid proxy for "player turn".
* **Mission records are stored as `msn_id : success : fail : time`.** There is a
  **fail** counter beside the success count — missions can be failed, which is
  independent justification for the abort/`Run` path in `DamageWatchdog`.
* **Not available from the client:** per-skill CP cost, cooldown length, damage
  and targeting mode all live in a runtime-populated `SKILL_DATA` (server-fed).
  `Skill.as` is only an asset loader. So per-slot costs and cooldown lengths still
  have to be measured in-game, one slot at a time, and recorded in the config.
  `SKILL_DATA.type` is validated against a `SkillData.ALL_NINJUTSU_TYPES` enum,
  which does confirm skills are categorised.

## Template discrimination matrix (measured offline, engine/bot.py)

Scored all 26 templates against 4 known frames with a 0.40-1.10 scale sweep.
This is the ground truth for state classification - do not guess thresholds.

| template | daily_popup | lobby | combat | loading | use |
|---|---|---|---|---|---|
| charge_btn | 0.429 | 0.445 | **0.949** | 0.429 | BEST combat gate |
| dodge_btn | 0.401 | 0.407 | **0.918** | 0.383 | 2nd combat gate |
| action_flag | 0.383 | 0.383 | **0.923** | 0.387 | combat |
| run_btn | 0.581 | 0.579 | **0.940** | 0.597 | combat |
| attack_btn | 0.372 | 0.351 | 0.791 | 0.318 | WEAKEST - do not gate on it |
| day_claimed_check | **0.992** | 0.589 | 0.526 | 0.644 | BEST daily-popup gate |
| day_current_pointer | **0.973** | 0.602 | 0.648 | 0.524 | daily popup |
| claim_daily | 0.791 | 0.407 | 0.407 | 0.352 | action target (peak ~0.79) |
| close_popup_x | **0.951** | 0.671 | 0.666 | 0.525 | popup |
| loading_text | 0.503 | 0.503 | 0.495 | **0.866** | loading (thr 0.80) |
| lobby_logo / nav_* | 0.87-0.97 | 0.87-0.98 | 0.87-0.96 | 0.29-0.56 | SHELL ONLY |

### Rules this establishes

* **The persistent shell is not a state discriminator.** `lobby_logo` and all six
  `nav_*` score essentially identically in the lobby, over a popup, and in
  combat. They separate "inside the game" from "loading" and nothing else.
* **There is NO positive lobby anchor yet.** Lobby is currently defined
  negatively (`lobby_or_shell`). The village labels are semi-transparent over
  animated art and unusable. A lobby-unique template still needs cutting -
  candidates: the right-side icon rail, or the "Season" text.
* **Gate combat on TWO corroborating command buttons**, not one. Prefer
  `charge_btn` + `dodge_btn`.
* **`click_to_continue` is unusable as a gate**: 0.642-0.849 across unrelated
  states, false-fires on combat. It caused every frame to misclassify as
  "cutscene" until removed. Needs re-cutting.
* **`close_popup_x_large` never fires** (flat 0.547 at every scale). Bad crop.
  Consequence: the Daily Login Calendar state is unclassifiable. Re-cut it.
* **`confirm_check` never fires either** (flat ~0.40). Unvalidated.

### Known scale problem

Two template sets exist at different canvas geometries: the Phase 1 + command
templates peak at **scale 0.46**, the later full-viewport combat captures at
**0.54** (~17% apart). Text templates lose ~0.4 confidence at 8% scale error, so
this is not survivable by threshold tuning. Either pin the viewport (what
`bot.py` does) so only one geometry ever occurs, or re-cut everything at one
canonical geometry. Until then the combat gate is scale-fragile.

## TP Training (Special tab) — measured by playing it

Path: Mission Room -> `Special` tab -> `TP Training`. Three per page, 2 pages.
Observed: Dangerous Potion / Secret TP Scroll / Weird Potion, all Lv 40,
XP 2000, Gold 2000, flame column showing **10**.

* The flame column shows 10 where story missions show `-`. The user states TP
  missions do not actually consume stamina; treat the displayed 10 as unverified.
* Detail panel is the same shape as story missions: `Completed: N`, back arrow
  bottom-left, green check bottom-right to start.
* Flow: green check -> cutscene ("click anywhere to continue", ~2 clicks) -> minigame.

### TP Training is FIVE missions in THREE minigame families

Measured live. Mission Room -> `Special` tab -> `TP Training`, 2 pages:

| page | mission | Lv | XP | Gold | flame |
|---|---|---|---|---|---|
| 1 | Dangerous Potion | 40 | 2000 | 2000 | 10 |
| 1 | Secret TP Scroll | 40 | 2000 | 2000 | 10 |
| 1 | Weird Potion | 40 | 2000 | 2000 | 10 |
| 2 | Another TP Scroll | 40 | 2000 | 2000 | 10 |
| 2 | The Kekkai in the Forest | 40 | 2000 | 2000 | 10 |

The names group into three families — **Potion** x2, **TP Scroll** x2, **Kekkai**
x1 — which matches "three kinds of minigame". Working hypothesis: **the name
prefix IS the minigame type.** Confirmed for Kekkai (below); the Potion and
Scroll games have not been opened yet.

The `Special` tab itself holds four entries: `Special Events` (greyed),
`Daily Mission`, `TP Training`, `SS Training`.

### Kekkai minigame — SOLVED live, and the counter mapping is measured

Played and beaten. `engine/kekkai.py` (solver) + `engine/kekkai_play.py` (live
driver). What the run established:

**Feedback mapping, determined by play rather than assumed:**

    GREEN disc = correct rune in the CORRECT PLACE
    GOLD  disc = correct rune in the WRONG PLACE

Both mappings were carried as live hypotheses and filtered until one died. The
history that settled it:

| guess | green | gold |
|---|---|---|
| Green, Red, Blue | 0 | 1 |
| Red, Black, Yellow | 2 | 0 |
| Black, Blue, White | 1 | 1 |

That leaves exactly ONE candidate under each mapping — `(Red,Black,White)` under
green=correct-place, `(Black,Yellow,Blue)` under the inverse. Submitting
`(Red,Black,White)` gave **"You break the seal!"**, then `Seals: 1 / 2`. So
216 candidates -> 1 in three guesses, solved on the fourth.

**Measured interaction:**

* six rune buttons, captured px at the standard 1720x720 viewport:
  Green (860,1076) Red (1018,1076) Blue (1166,1076) Black (1321,1076)
  Yellow (1486,1076) White (1639,1076)
* screen order matches the reference bot's rune list exactly
* filling the slots arms the kekkai centre — it turns dark red (#9C2F16, their
  bot waited on #7E1A01) — and **clicking that centre at (1259,513) SUBMITS**.
  Filling the slots alone does nothing.
* the "You break the seal!" dialog needs its green check acknowledged (found at
  scale 1.1, another size for that one glyph)

**History scroll must be LOCATED, not computed.** A fixed y0+pitch drifted
(measured y0 290 not 297, pitch 88.53 not 88.0 — ~25px over ten rows, enough to
read a neighbour's digit). Segment the green disc column instead: 10 rows,
y 290..1087, green x 1987, gold x +86.

**Counters are read by binarising the white outline.** The glyph is a dark digit
with a white outline on a coloured disc; thresholding bright pixels makes one
exemplar set serve both discs (self-match 1.000, cross-digit 0.161). Rows that
have NOT been played render dimmer, so each digit needs a played AND an unplayed
exemplar — the same "0" scored 1.000 against one and 0.767 against the other.

**Count filled rows by saturation FRACTION, not mean.** Parchment is itself
saturated: filled rows mean 87..93, empty 47..53, which no single mean cutoff
separates safely. `frac(sat>90)` gives 0.243..0.303 filled against 0.000..0.028
empty.

**Two bugs this run, both worth remembering:**
1. A guess entered by hand before the solver started occupied row 0, so reading
   "row N-1 for guess N" was off by one and fed guess 2's model with guess 1's
   feedback. Read the row found by counting FILLED rows, never by assuming.
2. `solve_live` reported "solved after 0 guesses" when the panel had simply never
   opened. Absence of the panel BEFORE any guess means not-open, not success.

**Locating a kekkai in the scene** needs shape, not just colour. A dark-red blob
search matched our own character's RED ROBE and clicked it, which did nothing
while the code reported success. Calibrated on a frame holding both:

| | area | bbox | fill | aspect |
|---|---|---|---|---|
| kekkai | 20622 | 481x268 | **0.160** | 1.79 |
| character robe | 4040 | 65x170 | 0.366 | 0.38 |

All three features separate them; the decisive one is FILL, because a kekkai is a
triangle OUTLINE and therefore sparse inside its bounding box while a robe is a
solid blob. `kekkai_play.find_kekkai` requires area >= 8000, fill <= 0.30 and
aspect >= 1.0, and is verified to fire on a kekkai frame and NOT on two
character-only frames.

**TRAVERSAL: run to a MAP EDGE, do not sweep the current map.** If no kekkai is
on screen, the way forward is to run to the left or right edge of the canvas —
**the location changes during the running sequence**. Clicking mid-ground points
just shuffles the character around one map forever and finds nothing; that was a
wasted attempt. At the standard viewport the canvas is captured x 760..2680, so
the edge targets are ~(800, 880) and ~(2640, 880), and a run plus the transition
needs a longer settle (~4.5s) than a short walk — scanning mid-transition reads
as "nothing here".

This is almost certainly the same mechanic behind `mission.traversal_click` being
unset for story missions: encounters trigger on movement, and movement means
running to an edge.

**HEADING COMES FROM WHERE YOU SPAWN.** You enter a map through one edge, so you
appear NEAR that edge and must run AWAY from it. Derive it per map from the
character's x against the canvas centre (1720 at the standard viewport):
x < centre -> head right, x > centre -> head left. Neither a fixed default nor a
merely persistent heading works — with the character at x=2268 a default of
"right" ran it straight back through the edge it had just come from, repeatedly.
`kekkai_play.heading_from_spawn` does this.

The character is found by the SAME colour pass as the seal, using the inverse
shape signature — area 4040, bbox 65x170, fill 0.366, aspect 0.38: small, tall
and solid, where a seal is large, wide and sparse.

**A seal is APPROACHED, then OPENED.** The first click walks you to it; only a
second click opens the puzzle. One click and a "did it open?" check is not enough.

**NODE COUNT IS THE CODE LENGTH.** A 3-node triangle seal is a 3-rune code; a
5-node pentagon is 5. Count the pale nodes inside the seal — and do it BEFORE
opening the puzzle, while the seal is still drawn in the scene. Counting after
opening returns nothing and silently falls back to the default length, which had
a 5-node seal being solved as a 3-rune code.

**Detector calibration — area and aspect, NOT fill.** Measured across two real
seals and the character:

| | area | bbox | fill | aspect |
|---|---|---|---|---|
| triangle seal (3 nodes) | 20622 | 481x268 | 0.160 | 1.79 |
| pentagon seal (5 nodes) | 32897 | 432x244 | **0.312** | 1.77 |
| character robe | 4040 | 65x170 | 0.366 | 0.38 |

A `max_fill` of 0.30 — fine for the triangle — REJECTED the pentagon, because
five big nodes fill more of the box than three. And the seal range (0.16..0.31)
now sits close to the character's 0.366, so fill is only a loose safety bound.
Area separates by 5x and aspect by 4.6x; use those.

**"Panel open" needs a plausible ROW COUNT.** One stray green blob is not the
history scroll. After a correct guess the panel closes instantly, and a single
unrelated green element made `find_rows` report "open, 1 row" — so the solver read
digits out of a closed panel, scored 0.000, and reported failure on a puzzle it
had just solved. Require >= 5 discs, and check for a closed panel BEFORE reading
digits after a submit.

### THE KEKKAI PANEL MOVES TOO — locate the runes, never assume them

`RUNE_XY` and `CONFIRM_XY` are a REFERENCE LAYOUT, not the truth. Measured on a
live frame, the whole puzzle sat ~116 px higher:

| | reference | actual | delta |
|---|---|---|---|
| rune Green | (860, 1076) | (876, 960) | (+16, **-116**) |
| rune White | (1639, 1076) | (1660, 960) | (+21, -116) |
| kekkai centre | (1259, 513) | (1261, 387) | (+2, **-126**) |

The rune discs are only **r ~55**, so a 116 px error puts every click clean
outside its button — and the failure is **completely silent**: no slot fills, so
nothing is ever submitted, so the history stays empty. The solver then read
"the last filled row", which with zero filled rows is row 0 — an **UNPLAYED**
row — whose dim `0 / 0` it could not classify. It reported a digit-exemplar
problem and burned the mission. The scroll showed **ten identical unplayed
rows**, which is the tell.

`find_rune_buttons` locates them with Hough circles instead. **It must PICK OUT
the row rather than take everything found:** 13 circles are present in that band
— the six rune discs (r~55), the history scroll's own counter discs (r~39) and
strays. Requiring "exactly six" simply failed and fell back to the reference
layout, i.e. straight back into the bug. Group by y, then find a run of six with
consistent spacing AND consistent radius; nothing else on that screen is a row
of identical circles.

`find_confirm_point` locates the submit disc as the large round dark blob in the
scroll (measured area 32695, bbox 207x201).

**And verify the guess REGISTERED — but only with the PANEL STILL OPEN.** After
submitting, if no new row appeared, the clicks did not land — abandon rather than
reading phantom rows. Reading an unplayed row is what disguised a geometry fault
as a digit-recognition fault for a whole mission.

**ORDER MATTERS HERE, AND GETTING IT WRONG THROWS AWAY WINS.** A correct guess
closes the panel instantly, and a closed panel has no filled rows — so asking
"did it register?" BEFORE the closed-panel check reports "the clicks did not
land" for a puzzle that was just SOLVED. Measured live, on the very sequence
this file already records:

    Green,Red,Blue     -> 0 green, 1 gold
    Red,Black,Yellow   -> 2 green, 0 gold
    Red,Black,White    -> panel closed  ->  "did not register", abandoned

`(Red,Black,White)` is the answer that produces "You break the seal!". The
solver had narrowed 216 candidates down to it correctly and then threw the win
away. This is the SAME trap documented just below for digit reading — check for
a closed panel FIRST — walked into again by a later edit one branch higher.

### THE TWO DISCS RENDER DIGITS DIFFERENTLY — harvest per (digit, disc)

This is why the digit reader keeps blocking a mission, and it is systematic, not
random. Observed across three separate failures:

    GREEN disc  ->  digits drawn as an OUTLINE   (seen: 0, 0, 0)
    GOLD  disc  ->  digits drawn SOLID/FILLED    (seen: 2, 3, 0-with-glare)

So the exemplar set needs BOTH forms per digit, and a green exemplar does not
help a gold read at all: green-0 against the gold-0s measures 0.503, 0.152 and
**-0.057**. Harvest with that in mind rather than one-per-digit.

**Normalising the two forms was tried and measured WORSE.** Flood-filling the
outline so it matches the solid gave 0.000 against 0.503 raw - the fill produces
degenerate near-uniform masks. Same outcome as the earlier glare-cleanup
attempt. Two forms it is.

Digit exemplars are still harvested by hand as new renderings appear (`0` now
has 10 variants, `1` three, `2` two, `3` one — and **4+ none at all**). A glare-cleanup
filter was tried and measured WORSE — dropping border-touching blobs removed
most of the digit too (match 0.501 -> 0.196), because the outline touches the
border as well.

### TP mission COMPLETED end to end

"The Kekkai in the Forest" finished by the bot: rewards banked (gold
1,196,781 -> 1,198,981, XP 494,230 -> 496,230) and the game returned to the
village. `engine/tp.py` does the whole flow — lobby -> Mission Room -> Special
tab -> TP Training -> start -> cutscenes -> hunt and solve seals -> acknowledge
Mission Success. Navigation templates `special_tab` (margin 0.660) and
`tp_training_row` (0.720) verified.

**It refuses the Potion and Scroll families by name.** Only Kekkai has been
opened and understood; starting one of the others would burn the stamina the
flame column claims to cost on a minigame we cannot finish.

**Mission Success can raise a "Share with Teammates!" dialog.** Close it with its
X. NEVER click "Share to wall" — that publishes to a social feed, which is not
something the bot should ever do unasked.

### Where the algorithm came from — the reference bot already had it

"The Kekkai in the Forest" opens with `Seals: 0 / 2` and a triangular kekkai
(kanji 封). Clicking the kekkai opens the puzzle, which states its own rules:
**"Unseal the kekkai by clicking the runes in order"**.

* N ordered slots (3 in this mission), numbered 1 2 3, with a clear button
* **SIX runes**: green spiral, red spiral, blue triangle, black lightning,
  yellow flame, white crescent
* a history scroll, one row per guess, **two counters per row**

Two counters per guess means Mastermind: (correct rune correct place, correct
rune wrong place).

**The reference bot solves exactly this** — for the Jounin and Sage exams, not
for TP. Its dict is literally called `jouninKekkai`. Its rune set is
`["Green","Red","Blue","Black","Yellow","White"]`, matching ours exactly, and it
supports code lengths 2..5. Algorithm (`-/-.cs` class `_2003`,
`FormMain.cs:11169`): precompute all candidate codes, filter to those consistent
with every past guess's feedback, return a survivor.

Ported to `engine/kekkai.py`, with two deliberate differences: we pick the
survivor that minimises the worst-case partition (Knuth minimax, capped at a
pool of 300 for cost) instead of `list[0]`, and repeats are allowed by default
since we have not measured whether the game's codes repeat a rune. Self-tested
exhaustively: every secret solved, length 3 avg 4.07 / worst 6 guesses.

**Still needed before it can run live:** the six rune button coordinates on our
geometry, and a way to READ the two feedback counters (the reference bot scrapes
them with dedicated routines). Without the counters the solver has no input.

### What the reference bot has for the OTHER minigames

Inventory, so this is not re-researched:

| solver | wired? | what it does |
|---|---|---|
| rune solver | **yes**, 2 sites | the Kekkai Mastermind, above — Jounin + Sage exams |
| `CardSolver` | **yes**, 1 site (`FormMain.cs:17661`) | 3-option "which matches", inside a battle loop; dual metric (greyscale + Canny diff); **guesses "A" on failure** rather than re-capturing |
| `BoardScanner` + `PipePuzzleSolver` | **no callers at all** | pipe-rotation puzzle, ~670 lines of dead code |
| `FormDailyTP` | n/a | ctor + `updateForm` only. Their TP mode just fights N battles — **no TP puzzle logic whatsoever** |

So for the Potion and Scroll families there is nothing to borrow; they have to be
solved from observation.

### Minigame dispatch is by OBSERVATION, not by a configured family

`engine/minigame.py` classifies what is on screen and dispatches. An earlier
`--family kekkai` flag was the fixed-script anti-pattern this file warns about,
and it is now advisory only — if the caller's label disagrees with the pixels,
the pixels win.

    kekkai      rune Mastermind, seal in scene or panel open   -> PLAYABLE
    seal_entry  hand-seal minigame                             -> recognised, DECLINED
    combat      a battle                                       -> handed to the battle runner
    unknown     cutscene / traversal / panel / lobby           -> nothing

Every criterion measured; verified on 9 frames across two canvas geometries.

**Two false positives had to be measured away, and both are instructive:**

1. **Village architecture reads as a seal.** On one lobby frame FIVE blobs cleared
   `area >= 8000`, with fills 0.286 / 0.310 / 0.358 — straddling the pentagon
   seal's 0.312. Fill cannot separate them. **Bounding-box HEIGHT can**: real
   seals measured 244, 268, 279 px tall; every lobby blob 84..161. A seal is tall
   AND wide, village art is flat.

2. **The combat target ring is geometrically indistinguishable from a seal** —
   measured area 11988, bbox 395x264, fill 0.115, aspect 1.50, which passes every
   shape filter a real seal passes. Shape CANNOT separate them, so context must:
   check for the command bar first via `BattleGeometry` and call it combat.

Also: every one of these colour-blob detectors needs its ROI CLAMPED to the frame.
Unclamped, a 1920-wide frame fed a region starting at x=1950 and OpenCV threw on
an empty slice.

### CORRECTION — the hand-seal (Potion) minigame IS solvable, and the answer is SHOWN

This file previously recorded the seal-entry minigame as unsolvable, on two
claims that a live observation disproves. Both are wrong, and the mistake was
WHEN we looked, not how fast:

    WRONG: "the two slots are card BACKS - they are the empty INPUT, not a
            revealed answer"
    WRONG: "a 47 fps burst over 5 s across the slot strip caught no reveal"

Nothing is revealed until **Start** is pressed. The earlier burst sampled the
pre-Start screen. Press Start and the phases are:

| phase | slots | ten tiles |
|---|---|---|
| idle (Start on screen) | face-down backs | face-down backs |
| **look** (~3 s "READY", then a 9..0 hourglass) | **FLIP OVER and show the two required seals** | face up but GREYED |
| input | flip back to backs | full colour, clickable |

So it is a **memorisation game with a generous look phase**, exactly as the user
said — not a 90-way guess against three lives. The old conclusion that it "needs
a jutsu -> seal-pair table that is not in the client" was answering a question
the game never asks: the required seals are shown to you, just not at the same
moment as the buttons.

**The reference bot has nothing for this.** `FormAnniversaryMinigame`,
`FormCrewMinigame`, `FormSoccerFeverMinigame` and `FormSSTraining` are all
settings forms — constructor plus `updateForm`, same as `FormDailyTP`. There is
no hand-seal logic anywhere in it.

### Measured geometry (captured px, the pinned 1720x720 viewport)

    Start button      (1740, 400)   tpl/tp_seal_start.png, 1.000 / 0.248 worst
    slot cards        (1651, 821) and (1806, 821), about 140x175
    ten seal tiles    x = 1051 + 150*i for i in 0..9, y = 1069, about 104x104
    "Skill : N / 4"   tpl/tp_seal_hud.png, 1.000 / 0.348 worst

Verified by overlaying the grid on a live frame: all ten boxes centre on their
tiles, both slot boxes on their cards.

### SATURATION DOES NOT SEPARATE THE PHASES — the blue glove does

This cost two live rounds. A **face-down card is orange flame art** and reads as
saturated as a live seal, so a saturation gate fired on a board that had not
dealt yet: once it concluded a round was already running and never pressed
Start, once it tried to read seals off card backs.

Only a live seal has a saturated **blue glove**:

| state | blue fraction |
|---|---|
| face down (flame back) | 0.000 |
| greyed during the look phase | 0.000 |
| live and clickable | 0.157 .. 0.264 |

`seals.tiles_live` and `seals.slots_revealed` both key on that, and all three
phases are pinned as fixtures (`ref/auto/tp/seal_facedown|look|active.png`).

### Matching a revealed slot to a tile — partly solved

The same seal is drawn **differently** in the two places: the slot card shows it
small over animated flames, the tile shows it filling a wooden frame — and during
the only window where both are visible, the tiles are greyed. So the pixels
genuinely do not correspond. Measured separation between the correct tile and
the runner-up, across the whole strip:

| metric | slot A | slot B |
|---|---|---|
| greyscale difference | 1.03x | 1.09x |
| Canny edges | 1.01x | 1.03x |
| grey+Canny (the reference bot's CardSolver metric) | 1.01x | 1.07x |
| dark-ink silhouette | 1.03x | 1.34x |
| normalised cross-correlation | 1.05x | 3.49x |
| **blue glove only, tight-cropped** | **1.10x** | **5.13x** |

The blue glove is the one element that survives every rendering difference: skin
tones collide with the flame background and the ink outline collides with the
wooden frame. Tight-cropping to its bounding box normalises position and scale
in one step. Connected-component isolation was tried and is worse.

**It is decisive for most seals and not for all** — two of the ten have
near-identical glove silhouettes (1.10x). A live attempt on a thin margin was
WRONG: one heart lost, the target skill rerolled, the board reset.

**The honest fix is a labelled catalogue** of the ten seals in BOTH renderings,
harvested once (`engine/seals.py --save-crops` writes to `ref/auto/tp/seals/`),
then matching by identity rather than by cross-rendering similarity. Until that
exists, `potion` is deliberately NOT in `tp.SUPPORTED`.

### THE SIGNS ARE SHOWN ONE AT A TIME, AND THE SEQUENCE GROWS

Two corrections to the section above, both measured live:

1. **The signs appear sequentially, not together.** Mid-reveal on a four-sign
   round the slot blue fractions read `[0.262, 0.182, 0.000, 0.000]` — two shown,
   two still to come. A single snapshot cannot read the sequence, and a gate that
   waits for every slot at once may never fire. `seals.capture_sequence` polls and
   keeps the FIRST frame in which each slot shows a sign, which also preserves
   the order — the thing the game actually tests.
2. **The number of signs GROWS.** `Skill : 1/4` shows two; `Skill : 3/4` shows
   four. Hardcoding two cost a mission: the bot entered two signs of a four-sign
   sequence and left the round half-entered, unrecoverable. `seals.find_slots`
   measures the row instead — the parchment behind it is a flat value 255 and a
   card is darker, so the dark band gives the count at the known 150 px pitch.
   The gate must be generous (230, not 170): the cards are not drawn alike, and
   one measured 190..215 while its neighbour was 81..132.

Note the band spans the *outer edges* of the first and last card, so its width is
`(n-1)*pitch + card_width`. Forgetting the card width over-counts by one.

### LEVEL UP — a screen nothing knew, and it got WALKED ON

A level-up panel appeared and no anchor covered it, so
`looks_like_mission_scene` found nothing to veto with and the runner "traversed"
on top of it — the log filled with `dead end (run 19)` while a Level Up panel
sat on screen. `level_up` is now in `NOT_IN_MISSION`.

The generic confirm rung could not have caught it either: **this check is drawn
at scale 1.5**, outside that rung's deliberately narrow 1.00..1.20 sweep. So it
gets its own rung sweeping 1.20..1.80, which resolves to (2502, 946) at 0.973.

**The panel ANIMATES for about 3 s.** The ladder's 1 s inter-step settle looks
again mid-animation and judges a half-played screen, so `Step` now takes a
per-rung `settle` and this one uses 3.2 s — rather than slowing every rung down.

The anchor is the words **"Level Up!"** only, NOT the level number beside them.
Same rule as `tp_seal_hud`: an anchor must not contain the thing that varies.
Measured 1.000 here against a worst negative of 0.508.

**The recurring shape, now four times over:** a negative definition
("no anchor matched, therefore scenery") licenses a blind click, and every new
UI surface it has never met becomes a new way to walk into furniture. Mission
list, battle-between-turns, seal-broken dialog, and now level-up.

### A LONE GREEN CHECK IS A DIALOG — the ladder acknowledges it generically

The kekkai's **"You break the seal!"** dialog stranded the bot: 20 unrecognised
frames and then a halt, with the check plainly on screen. It matched at
**0.975** — but at **scale 1.1**, and the ladder swept scales only for a rung's
TARGET, never for its ANCHOR. `Step(anchor_scales=...)` fixes that.

Rather than cut a template per dialog, accept the glyph itself: it is the same
check every confirm dialog uses. Two things make that safe:

* It sits LATE in the ladder. Everything with its own meaning — Mission Success
  (banks the reward), a Victory panel — is handled above and keeps its own rung.
* **A mission detail panel's control is the same glyph, and clicking it STARTS
  A MISSION.** So a `mission_list` rung (anchored on `list_back_arrow`, which
  fires on both the list and the detail panel) backs out FIRST. By the time the
  generic rung is reached, a green check can only be a dialog.

Verified on eight frames: seal dialog -> `confirm_dialog`; detail, list and
all-locked list -> `mission_list`; Mission Success and Victory -> their own
rungs; lobby -> arrival; **combat -> nothing at all**.

**Keep the anchor sweep NARROW.** A 21-scale sweep of a full frame costs ~1.5 s
and this rung is reached on exactly the frames the ladder cannot otherwise name,
so the expensive case would be the common one. The large sizes (1.18 Victory,
1.84 Mission Success) already have their own rungs; a dialog's check measured
1.10, so 1.00..1.20 in five steps is enough and 4x cheaper.

### NOTHING BELOW A 1720 VIEWPORT — the panel will cover the game

The page centres the game in the FULL viewport, ignoring the panel. With a
960-wide game and a 380-wide panel:

    centred game   (W+960)/2 <= W-380   ->   W >= 1720

Measured the hard way, after a 1440 option was offered in the panel: game
240..1200 against a dock starting at 1060 — a **140 px OVERLAP**, with the panel
drawn on top of the game and the no-click zone covering playable area. A test
now checks every offered size against the panel width, because an offered size
that breaks the bot is worse than not offering it.

### TRIED AND REVERTED: flush-lefting the game to remove the dead strip

Left-aligning would remove the wallpaper strip AND drop the floor to 1340
(`960 <= W-380`), so it is worth doing properly one day. The attempt broke the
game and was reverted.

**It was NOT a resize** — the iframe and the inner `ruffle-player` both stayed
960x839 with no width/height set. The fault was in `align()`: **both axis
corrections were computed from ONE rect measurement**, and changing `marginLeft`
REFLOWS the page, invalidating the `r.y` used a line later. Each pass
over-corrected and the margins compounded — `marginTop` reached **177 px** —
pushing the game down and clipping its top, which is where the panel tabs live.

Second trap, on the way back out: removing the code that SETS a margin does not
clear a margin already applied. The stale inline `-220px` / `177px` persisted in
the DOM and the game stayed broken until they were explicitly cleared and focus
re-applied from scratch.

To retry: re-measure the rect BETWEEN the two corrections, and converge each
axis separately with its own tolerance check.

### THERE IS NO TABLE OF WINDOW SIZES — one stage, two transform axes

The client's own AIR manifest (`ref/swf_assets/AIR_application.xml`) settles
this: `<resizable>true</resizable>`, `<maximizable>true</maximizable>`,
`<fullScreen>true</fullScreen>` around a single `<width>960</width>` stage, with
a comment noting the width/height tags were removed because fullscreen ignores
them. Every extracted asset is authored at 960 width (960x550, 960x780,
960x237, 960x32). So the game has ONE layout that is uniformly scaled — not a
set of per-size layouts to learn.

Measured on the live client, the two axes behave completely differently:

| change | game rect (CSS) | captured canvas | nature |
|---|---|---|---|
| viewport 1720 / 1440 / 1920 / 1280 | 960x839 always, x = 380 / 240 / 480 / 160 | unchanged | pure **OFFSET** — it RE-CENTRES |
| dpr 1 / 2 / 3 | 960x839 always | 960 / 1920 / 2880 | pure **SCALE** |

So `Capture.fix` is scale AND offset. **Scale about the GAME's origin, then
translate** — scaling about the frame origin instead smears the offset by the
scale factor, which looks right until it is hundreds of px out.

**Only dpr-2 sizes are offered in the panel.** The transform handles dpr 1 and 3
correctly for COORDINATES, but templates are cut at dpr 2 and are not re-cut,
and `matchTemplate` is not scale invariant - this file already measures
text-heavy templates losing ~0.4 confidence at 8% scale error. Offering a dpr
that clicks in the right place while recognising nothing would be worse than not
offering it. Scale the templates at load and re-measure the margins first.

Applying a window size RELOADS the game, so the panel arms on the first press
and commits on a second within 6 s, and the runner clears the drift cache and
re-arms alignment afterwards - stale hints aim every click at the old layout.

### A HALF-APPLIED DRIFT CORRECTION IS WORSE THAN NONE

The memory board stopped halfway with a confusing signature: **19 faces known,
11 pairs REFUSED, 10/19 cleared, "no proposable pair; stopping"**. It looked
like a matching problem. It was a coordinate problem, and a self-inflicted one:
the correction had been wired into the READ path and not the CLICK path.

    board_frame  ->  board_box(cap)   CORRECTED origin
    crop         ->  pos_xy(i)        RAW position   <- mixed space
    flip         ->  pos_xy(i)        RAW position   <- click 117 px out

Two consequences, and the first is what made it hard to see:

* `crop` subtracted a CORRECTED origin from a RAW position, so every cell crop
  was offset by the drift. The faces were still mutually distinguishable, so 19
  were "known" - but they were the WRONG faces for those indices, hence
  pairings the board refused.
* the flip clicked 117 px off, against cards ~150 px tall - the neighbouring
  row, or the gap between rows.

`pos_xy`, `crop`, `cell_state` and `identify` all take `cap` now, and every
geometry site in `play()` passes it. **One coordinate space, or none** - a
partial correction produces plausible-looking output and hides the fault.

`cards.py`'s solving logic is still untouched; only the coordinate space moved.

### One shared drift correction, rather than an anchor per minigame

`Capture.game_offset()` measures how far the game canvas has moved from the
layout every constant was cut at (`REFERENCE_ORIGIN = (760, 0)` captured px),
and `Capture.fix(x, y)` corrects a coordinate by it. It is cached for a second,
because it is a CDP round trip and callers may ask per click.

Use it wherever a HARDCODED coordinate is consumed — never on a point derived
from a template match or a live detector, which are already in current-frame
coordinates and would be corrected twice.

Wired into `cards.board_box` / `cards.cell_xy` and the `kekkai_play.locate_panel`
fallback. `ensure_focus` re-aligns every cycle so the correction is normally
(0, 0); this is the safety net for when alignment cannot hold, so a displaced
game degrades into slightly-off clicks instead of a cascade of subsystems each
blaming itself.

**A missing measurement returns (0, 0), never a guess** — an unlocatable game
must not be able to move a click.

### WHY IT NEEDED RE-ALIGNING AT ALL — the SITE moves the game, every reflow

Root cause, and it is not ours. The site's own JavaScript sets

    #game-container { position: absolute; top: -58.5px }

pushing the container up by the overflow — the game is 839 CSS px tall inside a
780 px wrapper. Focus mode counteracted it with a fixed `marginTop: 59px` on the
iframe, and **a fixed number only cancels that at ONE layout**. The site
recomputes its `top` on any reflow (the Admin Message banner, hiding siblings, a
container resize), so the game went out of place again and again — which is the
whole history of "the game drifted; re-aligned" in the logs.

**Ruled out first, by measurement:** the ancestors ARE flex-centred
(`display:flex; align-items:center`), but setting `flex-start` and removing the
margin entirely left the game still at -58. So centring was not the cause; the
absolute `top` was.

**The fix pins instead of chasing.** An `!important` STYLESHEET declaration
beats an inline non-important one, so the site cannot undo it:

    #game-container { top: 0 !important; }

Verified live: y goes to 0, and stays 0 after re-setting `top:-58.5px` inline —
exactly what the site does on reflow. `align()` re-asserts the rule because a
reload drops the injected `<style>`, and turning focus off removes it. Position
only, never a size: resizing `ruffle-player` desyncs click -> stage mapping.

The margin nudge is kept as a residual fallback, in case the container id ever
changes and the rule stops matching.

### THE GAME DRIFTS OUT OF ALIGNMENT, AND EVERY ABSOLUTE GEOMETRY GOES WITH IT

A one-shot align cannot hold. Focus mode top-aligns the game once, then the page
scrolls or the layout reflows and **nothing puts it back**. Measured with the
memory board on screen:

    scrollY 60,  game iframe at y = -118 CSS  =  -236 CAPTURED px

and the board's card rows measured **-237** from where `cards.ROWS` says they
are. That is the same number: the board had not moved, THE GAME HAD.

This is the single explanation behind a run of unrelated-looking failures:

* the memory board reported "board gone before flipping card 0"
* the kekkai rune clicks landed outside r~55 discs, filling no slots
* "could not find the Special tab" on a healthy Mission Room
* templates that fail at one moment and match at another

Every minigame's geometry is absolute, so a displaced game breaks all of them at
once, each in its own confusing way — and the error each one reports names its
own subsystem, never the real cause. The kekkai's -116 and the board's -237 are
not different bugs; they are the same drift measured at different times.

`ensure_focus` now calls `align()` EVERY cycle. That cannot reintroduce the
jumping that re-APPLYING focus caused, because `align` is a no-op when the game
is already in place — it returns "aligned" and touches nothing. `__nsbotAlign`
also resets `scrollX/scrollY` first, since `getBoundingClientRect` is
viewport-relative and a scrolled page would otherwise be "corrected" by moving
the margin instead.

**Before blaming a minigame's own logic, check `scrollY` and the game rect.**

### FOCUS MODE — and why it is a CORRECTNESS feature, not decoration

`engine/dock.py` can hide everything on the page except the game and pin it to
the top of the viewport. It is armed by default in `app.py` and applied as soon
as the game iframe appears (never before sign-in — hiding the login page would
leave the operator staring at nothing).

It is not just calm. **The page scroll drifts, and the game moves with it.**
Measured across one session: scrollY 458 -> 420 -> 301 -> 242. The game is 839
CSS px tall in a 720 px viewport, so 119 px is always hidden and the scroll
decides which 119. The consequences were not subtle:

* "could not find the Special tab" on a perfectly healthy Mission Room, because
  the tab was 157 px above the viewport
* the resume ladder halting on screens it knows, because their anchor was in the
  hidden band
* "the tiles never became active" on a board whose tiles were active and
  on screen a moment later

Focus mode hides the SIBLINGS of the game, so the layout reflows and the game
lands at the top with `scrollY` 0 and staying 0. **It never touches the game
element's size** — the final nudge is a `margin-top`, not a width or height,
because resizing `ruffle-player` desyncs click -> stage mapping inside the SWF.

Top-aligned, not centred: left alone the container centres the game and loses
59 px off the TOP, which is where panel tabs and headers live. Aligning the top
sacrifices the NPC rail at the bottom, which nothing here needs.

`Capture.scroll_game(frac)` remains as the fallback for when focus mode is off,
and the resume ladder alternates the scroll before declaring a frame
unrecognised.

### Focus mode must be read from the PAGE, not remembered

A reload does not remove the panel — `Page.addScriptToEvaluateOnNewDocument`
re-injects it onto the new document — so the dock PRESENCE check still passes
while the fresh document is **not focused**. `Runner.focus_on` is a Python-side
belief, and it stayed True across the reload, so the early return meant focus was
never re-applied. Measured right after a Relog: `__nsbotFocusOn` false and
`scrollY` **301**, which is precisely the drift this file warns about — the game
is 839 CSS px tall in a 720 px viewport, so 119 px is hidden and the scroll picks
which.

`ensure_focus` now reads `__nsbotFocusOn` each cycle (one cheap evaluate) and
applies focus only when the PAGE says it is off. That keeps the convergence
property that matters: re-injection alone never triggers a re-apply, which is
what used to make the game jump around and the state read "unknown".

**The general rule, and this is the third instance of it in this project:** any
cached belief about page state — the no-click zone, the focus flag, a geometry
hint — is invalidated by a navigation, and the cheap fix is to ask the page
rather than to remember. A guard that defends where something *used to be* is
worse than no guard, because it reads as protection.

### The HUD anchor template included the COUNTER — re-cut it

`tp_seal_hud` was cut from a board reading "Skill : 1 / 4", digits included. The
moment a mission read "2 / 5" it scored **0.791**, under its own 0.88 gate, and
an entire mission was abandoned with "the hand-seal board is gone" while the
board was plainly on screen.

Re-cut to the invariant "Skill :" only, it scores **1.000 on 1/4, 2/5 and 3/4
alike** against a 0.356 worst negative. `HUD_REF` moved to (1028, 255) with the
crop.

General lesson, and it applies to every template in this project: **an anchor
must not contain the thing that varies.** A counter, a level, a name or a score
baked into a crop turns a state detector into a detector of one particular
value of that state.

### A round is not a mission

`Skill : N / 4` (sometimes N / 5) means the board must be beaten several times.
Playing one round and then closing out produced "close-out timed out after 45s"
on a mission still in progress. `seals.play` loops until the board is gone.

### STILL OPEN: the slot row is both the prompt AND the input

The row of slot cards shows the sequence during the look phase, and then shows
what YOU have entered. Those are different meanings for the same pixels, and
telling them apart is unresolved. It shows up as "recorded 5 of 6 sign(s)" on a
loop: some of those slots hold signs the bot itself entered on a previous
attempt, so waiting for the rest to be revealed waits forever.

Resolving it needs a phase signal that does not come from the slots — the Start
button's presence and the tiles' greyed/live state are the candidates.

### CONFIRMED: the hand-seal mission COMPLETES

"Weird Potion" finished by the bot with the anchored geometry — including
**four-sign rounds read in order** — then Mission Success banked and the village
regained. A representative round:

    panel offset (0, -111) (HUD at (1106, 144))
    sign 1..4 of 4 shown
    sign 0 -> tile 5 (d=0.077, margin 5.06x)
    sign 1 -> tile 2 (d=0.073, margin 3.06x)
    sign 2 -> tile 0 (d=0.055, margin 5.35x)
    sign 3 -> tile 4 (d=0.040, margin 5.06x)

Note the offset: the panel was 111 px from where it sits in the reference layout,
and the match margins were unaffected. That is the anchor doing its job.

**A failed TP mission is NOT consumed** — it stays in the day's list and can be
retried.

### The TP pass halts on screens the ladder cannot read — relog there too

A TP mission that does not close out calls `_recover_to_lobby`, which climbs the
resume ladder. But the ladder deliberately does not classify a battle, a
traversal map or a half-played minigame, so ending on one leaves it nothing to
climb: it burns its 20 unrecognised frames, halts, and the whole pass stops
**with the mission still playable on screen** — observed at `Seals: 1 / 2`.

The relog rung in `Runner.step` does NOT cover this, because the halt comes from
the Resumer's own `run()` inside the task. `_recover_to_lobby` takes a `relog`
callable and, on a halt, reloads ONCE and climbs again. Bounded deliberately: a
screen that survives a reload is a human's problem, not something to loop on.

**General shape of this bug: a recovery path that exists in two places, only one
of which was taught the new trick.** Same as the character finder below.

### THE CHARACTER BAND HAS A TOP AS WELL AS A BOTTOM — rooftops qualify

Traversal clicked `(800, 292)` — up among the buildings, not on the path — so
the run did nothing and logged a dead end. The finder had returned
`character at (2400, 292)`, identical every pass, which is the static-object
signature. A character stands on GROUND; village architecture is saturated and
tall, so it passed every other filter.

    real characters (every committed frame)   y 487 .. 805
    live mis-picks                            y 237, 292

`CHAR_BAND` floor is 400, between them with ~90 px of margin either side. A
character that genuinely stands higher now yields None, which falls back to
alternation — the safe failure.

**ONE CALLER WITH DIFFERENT ARGUMENTS IS THE SAME BUG AS TWO IMPLEMENTATIONS.**
Fixing the band exposed the divergence again in a new form: the runners shared
the algorithm, but `kekkai_play` still passed its own y band `(200, 1150)` and x
range `(800, 2650)`. On a village frame mission traversal correctly returned
None while the Kekkai runner returned a ROOFTOP at (2604, 542) — and the x range
mattered too, because it changes which blobs merge at the ROI edge. The wrapper
now overrides NOTHING, and the test asserts that by reading its source, so the
arguments cannot drift apart again.

**And `find_figures` needed its OWN band.** It was sharing `CHAR_BAND`, and its
mask is built from the ROI's own background MEDIAN — so narrowing the band did
not merely crop the search, it changed the background estimate and an enemy at
y=460 stopped passing at all. A change to the character finder silently broke
enemy detection. They answer different questions:

    CHAR_BAND (400, 950)   where OUR character can STAND
    FIG_BAND  (200, 950)   where ANY figure can be - enemies sit further back
                           and higher by perspective (one measured at y=460)

### TWO FINDERS FOR ONE IDEA — the seal hunt could not steer

`kekkai_play` had its own `find_character`, still keyed to a RED robe, while
mission traversal had been fixed to find a purple-robed character by saturation.
It returned None on every real frame, so `heading_from_spawn` fell back to
"right" and ran the character back through the edge it had just entered by —
the seal hunt "getting stuck wherever movement was necessary".

The finder now lives in `perceive.find_character` and both runners delegate. The
test asserts the two runners **AGREE** rather than that each works, because
agreement is what actually failed.

Measured on the live frame it stranded on (`ref/auto/tp/kekkai_seal2_hunt.png`,
"Seals: 1 / 2", no seal on screen): character (2231, 605), right of centre,
heading **left**. The old finder returned None there.

### The resume ladder needs a CUTSCENE rung

A failed mission ends on "Aww... you better take some rest..." over a
"click anywhere to continue" screen, and the ladder halted there after 20
unrecognised frames. That halt was correct — it refuses to click blindly — but it
could not get home from a screen whose only exit is a click, so the whole session
was stuck until a human intervened.

This file warns that `click_to_continue` was unusable as a gate (0.642..0.849
across unrelated states, false-firing on combat). **That was a different, badly
cut template.** `cutscene_continue` measures 0.968 positive against a 0.381 worst
negative across seven reference frames — a margin of 0.587 — so it is safe where
the old one was not.

It sits AFTER the result panels deliberately: a Victory or Mission Success panel
must be acknowledged by its own green check, not clicked through as a cutscene.

### GEOMETRY MUST BE ANCHOR-RELATIVE HERE TOO

**The whole panel moves.** After a page reload the Start button went from y=400
to y=432, every tile and slot crop moved with it, and the match margins collapsed
from 7..14x to 1.0x — the solver picked wrong twice in a row on a board it had
been reading perfectly a few minutes earlier.

This is the same rule the combat section already states: *battle geometry must be
anchor-relative, never absolute*. The anchor here is the **"Skill : N / 4" HUD**,
because it is present in EVERY phase (Start is not) and lands at exactly
`(1106, 255)` on every correctly-aligned frame. `seals.anchor_offset` returns the
delta and everything is computed from it.

Proven by shifting a frame and re-running the match:

| | picks |
|---|---|
| baseline | tile 7 @ 1.10x, tile 1 @ 5.13x |
| shifted 20px, **anchored** | tile 7 @ 1.10x, tile 1 @ 5.13x |
| shifted 20px, naive | tile 6 @ 1.18x, tile 6 @ 1.16x — wrong, and both signs collapse onto one tile |

### What the matcher actually achieved

On a correctly-aligned board the bot played **three rounds in a row with zero
mistakes** (`Skill : 3/4`, all three hearts intact), with first-sign margins of
7.03x, 14.53x and 12.89x and second-sign margins of 2.54x, 2.43x and 2.92x. The
blue-glove metric is good; every failure since has been a geometry or
sequence-length bug, not a matching one.

**A caution about "verification".** An attempt to confirm each pick by re-reading
the slot after clicking reported `d=0.000` — identical images — because the slot
had not changed yet. That is a vacuous check, not a passing one: a distance of
exactly zero between two captures means nothing happened, and should be treated
as a failed observation rather than a match.

### ABSTAINING STRANDS THE ROUND — this game inverts the usual rule

Everywhere else in this project the right move when unsure is "do not click".
**Here that is wrong, and it was measured.** Once the look phase has passed the
game parks the round waiting for two clicks: the slots are face down, there is
no Start button, and nothing re-triggers a reveal. Abstaining does not cost one
round of four — it strands the round permanently and the mission can never
finish. The only exits are a right answer or a wrong one.

So `seals.play_round(commit=True)` (the default) plays its best guess and logs
how confident it was. `commit=False` is for harvesting crops only.

The decision point that IS free is **before pressing Start**. Nothing is lost by
declining to start a round.

### Misc

* A miss costs one heart, **rerolls the target skill**, resets all ten tiles and
  both slots, and leaves `Skill : N / 4` unchanged — so a miss costs a life but
  not progress.
* Target skills seen: `Refresh`, `Water Burst`, `Lightning Edge`,
  `Fiery Spike Wheel`.
* Frame-differencing is useless on this screen: the training dummy animates
  continuously, so every frame differs regardless of events. Read content.
* Capture ceiling over CDP measured at ~16 fps full-frame, ~82 ms/frame.


### TP geometry — SUPERSEDED, kept only as a warning about viewports

An earlier pass recorded the hand-seal geometry at viewport **960x839 / dpr 2**,
in CSS coordinates: 10 seals at ~148 px pitch from x=147 to x=813 at y=540, slots
~(447,412) and ~(521,412), Start (488,200).

**Do not use those numbers.** Everything in this project is now measured at the
pinned 1720x720 / dpr 2 viewport in CAPTURED pixels, and the current hand-seal
geometry is in the section above. The two are recorded together only to make the
point that a coordinate is meaningless without the geometry it was measured at —
the same ten tiles are 148 CSS px apart in one and 150 captured px apart in the
other.

## Cross-reference: CMMhero NS Bot (decompiled) — SOURCE NO LONGER KEPT

Third-party Windows/C#/Adobe-AIR bot for a **different** private-server clone,
used as a mechanics reference. **`ref/tp/cmmhero` has been deleted**; the
findings below are the whole of what it was worth, the one algorithm worth
having is ported (`engine/kekkai.py`), and every open question that needed the
C# has since been answered by measuring our own client - see
`docs/PORT_FROM_CMMHERO.md` for which and how.

That is the better outcome regardless: their build is a different private
server, so their code was only ever a hypothesis about ours. Every place we
took their word for something and later measured it, the measurement won - the
target ring being the clearest case.

It was also mildly hazardous to keep on disk: it hardware-fingerprints, plants
a DPAPI licence file that survives uninstall, and opens a plaintext WebSocket to
a hardcoded IP. It was never run here.

Findings that changed our design, kept because the reasoning still applies:

* **Their symbol matcher is better specified than our sketch**
  (`CardSolver.cs:139-166`): inset each crop by 20% to drop the frame, resize to
  70x70, greyscale, then distance = `mean(|grey diff|) + mean(|Canny edge diff|)`,
  argmin wins. The **dual metric** is the transferable part - edges survive
  brightness/shading shifts, greyscale catches fill differences. Use this for seal
  matching rather than plain correlation.
* Cards located by exact-colour `InRange` + `ConnectedComponentsWithStats` with an
  **area filter** (900..3000 px), grouped into rows by Y proximity (<40 px), sorted
  by X. Cheap and template-free.
* On solver failure they **guess option 1** rather than re-capturing
  (`FormMain.cs:17661-17685`: the `null` case shares the "A" branch). We should
  retry the capture instead.
* **Their TP content is not ours.** Our mission names appear in none of the 1,734
  recovered strings, and `FormDailyTP.cs` is only a settings form (battle-limit
  checkbox + count). Their TP mode is "fight N battles" - it does not solve a seal
  puzzle, so their code cannot answer our open TP question.
* **Targets are eight fixed battlefield slots `T1..T8`** (two rows of four), never
  sprites or name plates. Strong candidate explanation for why our sprite/plate
  clicking was inconsistent: we were aiming at art, not at the slot. Re-measure on
  our client before use - their client is ~800x440, ours 960x720.
* **No HP/CP reading anywhere.** `FindAllInRange` has no callers outside
  `PixelSearch.cs`; `FindPixelColorRange` has one thin wrapper
  (`FormMain.cs:14479`). Our `bar_fill_ratio` work is not redundant.
* **No round/turn counter** (zero refs in `FormMain.cs`) and no flee/run path.
  Their only failsafe is a wall-clock **"Stuck Timeout"** (" stuck more than 3
  times"), which is time-based and would NOT catch a regenerating enemy - the
  screen keeps changing while the fight stays unwinnable. Our progress-based
  `DamageWatchdog` covers a gap their design misses.
* Cooldown detection abandoned: `CheckSkillCD` is stubbed `return true`
  (`FormMain.cs:6904`). They rotate a used skill to the back of a queue instead -
  zero calibration, but weaker than round bookkeeping. Useful fallback for slots
  whose cooldown length we have not measured.
* `Village (46,90,#003A8F)` - a positive lobby anchor as a **single pixel probe** on
  solid chrome. Cheaper than our template and sidesteps the semi-transparent-label
  problem entirely. Worth trying on our client.
* Correction to that folder's own notes: `PixelLoop2` (`FormMain.cs:14801`) is
  **not** a tolerance/neighbourhood variant. It calls `PixelFound` with exact
  equality; the difference is that it races all conditions concurrently
  (`Task.Run` + `Task.WhenAny`). It does not help with animated art.

## Traversal: finding and REACHING a unit (measured, and one silent typo)

Story-mission traversal is "run to a map edge until something ambushes you", but
maps also hold units standing in plain sight. Engaging those is what looked
"clunky", and there were three separate faults behind it.

**1. THE DETECTOR NEVER RAN. `self.cap` does not exist — it is `self.capture`.**
`find_moving_figure` opened with `cap = cap or getattr(self, "cap", None)`, and
`MissionRunner.__init__` assigns `self.capture`. `getattr` with a default does
not raise, so the detector returned `None` on **every frame ever captured**,
silently, for as long as it existed. Six sites had the wrong name, including the
`find_character` calls, which meant the canvas correction was also quietly
falling back to reference constants.

Symptom: the log contained no `something MOVED at` line at all, while a live
measurement on the same screen found a moving blob of area 7149 at (2478, 496),
about 10 px from the real enemy. It read exactly like "the bot cannot see
enemies", and it was not a perception problem in any sense.

**The unit test set `inst.cap` too, so it passed green against the bug.** A
fixture that hand-builds an object reproduces whatever name the code uses; it
cannot notice that the name is wrong. The suite now asserts that every capture
lookup in `mission.py` names an attribute `__init__` actually assigns.

General lesson: **`getattr(self, "x", None)` converts a typo into a permanent
silent negative.** Anywhere a detector may legitimately return "nothing", a
missing attribute and a genuine absence become indistinguishable.

**2. A LIVE TARGET WAS BEING BLACKLISTED AS SCENERY.** `_is_dud` and the
"6 failed engagements, walk instead" cap both exist to stop the bot chasing
shrubs — one bush was clicked 70 times. Applied to a *moving* target they do the
opposite of their job: one 6.1 s timeout retired the only real enemy on the map,
after which the detector had nothing to return and the bot went back to
edge-running. That is the "randomly moving, never going to the target" report.

**A thing that animates is alive.** Movement-sourced targets are exempt from both
the dud set and the cap; a failed approach means the walk needed longer, not that
the target was imaginary.

**3. ONE CLICK AND A STOPWATCH IS NOT AN APPROACH.** Walking across a map takes
longer than one `traverse_settle` (6.1 s), so the runner declared failure while
the character was still on its way. This is the same two-step the kekkai section
already records — *a seal is APPROACHED, then OPENED* — and it applies to units:
the first click walks you there, contact starts the fight.

So engaging is now **progress-based, not time-based**: click, wait for a combat
anchor, and if none came ask whether the character got CLOSER (Manhattan distance
to the target, needing > 20 px of improvement). Closing means the walk is working
and earns another click, up to `ENGAGE_TRIES = 3`. Not closing means the click
never took, and there is no point spending two more.

Clicks aim at the **feet** (`cy + bh // 3`), not the sprite centre: a walk-to
click wants the ground the unit stands on, while staying inside the sprite so the
character walks to the unit rather than to open ground.

**4. A NEW AREA IS RESCANNED IMMEDIATELY.** On `moved on`, the runner used to
return and walk again on the next pass before ever looking at where it had
arrived — so it could stride straight past a unit standing in the open. It now
rescans on arrival, and **clears the dud set**, because those coordinates
described the previous map and would blacklist innocent ground on a map the bot
has never seen.

## MINIMISING THE WINDOW — the renderer survives it, the COMPOSITOR does not

"Can the window be minimised and still keep the game running?" splits in two,
and rAF alone gives the wrong answer to the half that matters.

**THE GAME KEEPS RUNNING, COMPLETELY UNTHROTTLED.** Measured with a
second-by-second rAF trace recorded IN the page (so nothing is evaluated while
the window is down, and a suspended renderer cannot hang the probe):

    t     top fps   game fps   visibilityState
    -1      120       120      visible
    +1      119       119      hidden        <- minimised
    +6      118       118      hidden
    +12     120       120      hidden
    +13     120       120      visible       <- restored

`visibilityState` does go to `hidden`, and rAF does not care: 120 fps
throughout, in the top page and in the game frame alike. That is the three
launch flags doing their job — `--disable-backgrounding-occluded-windows`,
`--disable-renderer-backgrounding`, `--disable-background-timer-throttling`,
all three verified present on the live browser. `browser.py`'s comment
predicted exactly this and it is now measured rather than hoped.

**BUT THE BOT DOES NOT SEE VIA rAF — IT SEES VIA `Page.captureScreenshot`,
AND THAT BLOCKS.** Same window, captures issued on their own CDP connection
from a worker thread:

    t       took    phase
    -0.4    0.14s   visible
    +0.3    7.00s   MINIMISED   <- returned only when the window came back
    +8.2    0.13s   visible

    and again over 45 s down:
    +0.3   45.98s   MINIMISED   <- same, so it does NOT recover on its own

One capture, issued just after minimising, blocks for exactly as long as the
window is down. **Zero captures complete while minimised**, at either
duration. A minimised window does not composite, so there is no new frame to
hand over — and nothing times out, because `CDP.call` waits with `select` and
no socket timeout (a timeout expiring mid-frame desynchronises the stream, as
this file records).

**IT IS THE GOOD FAILURE, AND THAT IS WORTH SAYING PRECISELY.** The bot is
parked INSIDE the capture call: it never receives a frame to be wrong about,
never reports an unrecognised screen, and never clicks. The DANGEROUS version
would be captures SUCCEEDING with stale pixels, which is exactly what this
file warns about for the lock screen — "captures show a stale frame and every
template match is against the past". That is not this.

It freezes more than the perception, though: `Capture.on_activity` is the hook
that pumps the operator's buttons and the panel's heartbeat, so Run, Stop and
the staleness banner all stop being serviced too. Everything resumes cleanly
on restore.

**THE REAL HAZARD IS THAT THE GAME DOES NOT FREEZE WITH IT.** rAF keeps
running at 120 fps, so the world moves on while the bot is blind, and several
things in this game are on a CLOCK: Balance Control allows 169 s per stage,
Lights Out 84 s, and combat has its own turn timer. Minimise mid-puzzle and
the bot resumes to a `Mission Fail` it could not have prevented. Park the
window off-screen instead.

**OFF-SCREEN IS THE ANSWER, AND IT WORKS PERFECTLY.** Move the window instead
of minimising it: still a normal, composited window, just not where anyone is
looking. Measured over the same six seconds:

    +0.5 .. +5.3   0.14 .. 0.16s per capture, every frame different

Full speed, no stalls. Note macOS CLAMPS the position — asking for
`left: 6000, top: 3000` landed it at `1688, 995`, keeping a corner on the
desktop — so in practice the window ends up parked in a corner rather than
truly gone. Covering it with another window is the same class and the
occlusion flag already handles that.

Two things that follow:

* **Never resize it to hide it, only move it.** The bot's viewport is pinned
  with `Emulation.setDeviceMetricsOverride`, so it is independent of the OS
  window and moving cannot desync a click. Resizing `ruffle-player` is the
  standing prohibition at the top of this file, and shrinking the OS window is
  a needless invitation to reflow the page under the bot.
* **A separate macOS Space is UNMEASURED.** It plausibly behaves like a
  minimise (the compositor for an inactive Space may stop), and guessing is
  what this file exists to prevent. Measure it with the same probe before
  relying on it.

Still distinct, and still true: SLEEP cannot be prevented and the LOCK SCREEN
is unmeasured — see the section on those. This entry is about minimising only.

## SLEEP AND LOCK SCREEN ARE DIFFERENT PROBLEMS — one is impossible

Asked as one question ("can the bot keep running when I sleep or lock the
Mac?"), these have opposite answers, and conflating them wastes effort on the
half that cannot be fixed.

**SLEEP CANNOT BE PREVENTED, AND SHOULD NOT BE.** A user-requested sleep
suspends the CPU; every process stops mid-instruction. `caffeinate` asserts
against *idle* sleep only — it has no power to veto a sleep the operator asked
for, and no userspace program does. `engine/presence.py` therefore stops the
machine *idling* out (display sleep, auto-lock, Teams going Away) and is
irrelevant to a deliberate sleep.

**But waking up IS handleable, and the failure mode is nasty without it.** The
process resumes exactly where it left off, so nothing raises — while the game
has been disconnected from its server for however long the lid was shut. Every
cached geometry hint now describes a screen that no longer exists, so the bot
carries on clicking a dead canvas with complete confidence.

**Sleep is detectable, precisely.** On macOS `time.monotonic()` does not tick
while suspended but the wall clock does (re-read from the RTC on wake), so

    wall_delta - monotonic_delta  ==  seconds spent asleep

**Wall clock alone cannot do this**, and that is the whole point: a mission
legitimately blocks for minutes, so a wall-clock threshold would relog on every
slow mission — and a relog throws away an in-flight one. Measured awake, the
difference is `-0.0000s`; the gate is 60 s. `Runner._slept_for` returns it, and
waking relogs and clears the drift cache and alignment flag, exactly as the
window-size path does and for the same reason. It degrades safely: on a
platform whose monotonic clock includes suspend time the difference stays ~0
and the detector simply never fires.

### LOSING THE IDLE POKE MUST NOT ALSO LOSE SLEEP PREVENTION

`presence.KeepAwake` had ONE mechanism and it needs a permission. The fn-key
poke goes through `osascript`, and on a machine without the Accessibility
grant macOS refuses it — *"not allowed to send keystrokes (1002)"* — after
which the handler disabled keep-awake **entirely**. That threw away the half
that costs missions. Measured the morning after, from the bot's own log:

    the machine was asleep for 1172s - the game session will not have
    survived that; relogging          ... and again at 205s, 245s, 244s

Every one of those is a lost in-flight mission, and it looked like a sleep
problem the bot could not do anything about. It was a permission problem
taking an unrelated guard down with it.

Two concerns, and only one of them needs permission:

    caffeinate -i -w <pid>   prevents idle SYSTEM SLEEP. No permission of any
                             kind. This is what stops the suspend-and-relog
                             loop above.
    osascript key code 63    resets the HID idle timer, which is the only
                             thing that keeps the screen unlocked and Teams
                             off Away. Needs Accessibility.

**`caffeinate` cannot substitute for the poke, and that is measured** — this
file already records `caffeinate -u -t 1` moving the idle counter
39.3s -> 40.4s, i.e. not resetting it at all. It is a power assertion, not an
input event. So a machine with no Accessibility grant now farms through the
night and still goes Away in Teams, which is the honest trade; the warning
names which half is gone and which is still standing, instead of the old
"keep-awake disabled" that implied both.

**`-w <pid>` is what makes spawning a child acceptable at all.** This module
explicitly rejects "shelling out to an external daemon" because `kill -9` on
the bot — the operator's habit when a run wedges — skips every `finally` and
would leave the machine awake indefinitely with nothing left to turn it off.
`caffeinate -w` releases its assertion when the watched process exits, so the
guarantee is structural rather than a promise about cleanup paths. Verified
both directions: it holds while its target lives, and exits on its own when
the target dies.

**LOCK SCREEN IS A DIFFERENT ANIMAL — processes keep running.** macOS does not
suspend on lock; the risk is Chrome, not the OS. Ruffle's render loop is
`requestAnimationFrame`-driven, and Chrome suppresses rAF entirely for an
occluded window — **measured at zero callbacks per 1500 ms**. A frozen rAF
means the SWF stops advancing, so captures show a stale frame and every
template match is against the past.

`browser.py` already launches with the three flags that address it
(`--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding`,
`--disable-background-timer-throttling`), and the running Chrome was verified
to carry them.

**Whether that is SUFFICIENT under a locked screen is not yet measured**, and
it cannot be measured from a session that needs the screen in order to look.
So `engine/awake_probe.py` records it instead: it installs an rAF counter in
the page and samples the rate, `visibilityState` and focus every 2 s to
`run/awake_probe.log` with wall-clock stamps, so the locked window is
identifiable afterwards. Lock, wait, unlock, read the rows.

    fps holds near the display rate  -> the lock screen is survivable
    fps drops to 0.0 and recovers    -> the renderer was suspended

`--fix` additionally asserts `Emulation.setFocusEmulationEnabled` and
`Page.setWebLifecycleState("active")` — both confirmed present in this Chrome's
protocol — so the two runs can be compared rather than argued about. **Those
are deliberately runtime CDP calls, not launch flags:** a relaunch would cost
the session cookie and therefore a manual sign-in, which this file already
records as unrecoverable by the bot.

It reads a COUNTER rather than taking screenshots on purpose. A second CDP
client taking clipped screenshots re-applies device metrics and resizes the
page under the bot — that is what pressed Relog and dropped the session once
already.

## THE REFACTOR: one entry point, one declaration of a task

The bot "felt like several programs" for two concrete reasons, and neither was
the number of library modules — those form a clean layered DAG with no cycles
and each earns its place.

**1. FOUR RIVAL `__main__` ENTRY POINTS.** `app.py` was live; `dashboard.py`
(707 lines + `dashboard.html`), `overlay.py`, `run_mission.py` and `bot.py`
were earlier whole-bot front ends, superseded by `dock.py` + `embed.html` and
untouched for 7–9 days. README documented only `app.py`. Each could attach to
the same game as the live bot and click it.

`bot.py` was the instructive one: 389 lines of dry-run observation loop, of
which the real bot imported **one 25-line function**, `load_templates`. It now
lives in `perceive.py`, where template LOADING belongs next to template
MATCHING, and `bot.py` is gone.

Deleted: ~1,150 lines. Kept: the genuine one-off instruments
(`mint_template.py`, `swf_extract.py`, `calibrate.py`, `measure_ruffle.py`,
`awake_probe.py`, `capture_mission_panels.py`) — those are measuring and
harvesting tools, not rival bots. The test that separates the two categories is
whether a file has its own `__main__` that drives the GAME: a rival front end
can attach to the same session as the live bot and click it, an instrument
cannot. `capture_mission_panels.py` is imported by nothing and has no
`__main__`, so it is dead weight rather than a hazard — left in place because
re-deriving a fixture harvester costs more than the 160 lines it occupies.

**2. THERE WAS NO SUCH THING AS A TASK.** `step()` was an if/elif over four
task-name strings, and `farm_missions`' pre-flight (am I already in a mission?
is this a traversal map?) was inlined into the GENERIC path before the ladder
ran. Adding a task meant editing `step` in two places, plus `TASKS`, plus the
reset logic, and each task hand-set `mode` and `note` on its way out. Nowhere
did the code state what a task WAS.

`engine/tasks.py` states it once:

    preflight(rt) -> bool    handle this cycle BEFORE the ladder; True means
                             handled. "I am already in a mission" belongs
                             here, because only the task knows whether it can
                             start from the middle of its own work.
    run(rt) -> str|None      one cycle from the lobby; the note for the panel
    oneshot                  finishing is an ENDING, not a lap
    needs_lobby              False skips the ladder (only `idle`)

`TASKS` is now derived from the registry, so the panel and the loop cannot
disagree about what exists. The suite asserts `step()` mentions **no task by
name**.

### WHY SWITCHING TASKS FELT LIKE FIGHTING THE PROCESS

Three things with completely different natural lifetimes were welded to one OS
process:

| | lifetime | note |
|---|---|---|
| the page session | long-lived, **irreplaceable** | the bot cannot recreate the session cookie; only a human can sign in |
| the panel | survives the process | it is injected into the PAGE — which is why a kill leaves a zombie panel reading "no bot attached" |
| the task | should be seconds | cheap, interruptible |

So changing the **task** meant killing the **session**. That is the whole of
the jankiness, and it is not a file-count problem — merging every module into
one file would not have moved it at all.

A task is now a value the supervisor swaps. The supervisor owns the connection
and the panel for the whole session; a one-shot that finishes hands back and
the mode becomes **`ready`** — attached, holding the session, nothing running.
Deliberately NOT `paused`: paused reads as a warning, and this is not one. The
panel styles it its own colour, and the suite checks the panel can render every
mode `step()` can set, because a mode the panel does not know renders unstyled.

A one-shot must not re-run itself: a TP pass that looped would re-walk a list
it had just measured to be finished.

## TP: THE DAY'S LIST DECIDES WHEN A PASS IS OVER, NOT A COUNT

`run_all` had `max_missions=6` and a docstring asserting that **"completed
missions drop out of the day's list"**. They do not. They stay listed and go
GREY. Both halves were wrong, and together they produced "it never finishes
them all": a count is not the thing that decides whether a list is done, and
the pass could stop with startable missions still on screen.

There is no mission cap now. The pass keeps taking startable rows until every
row is either played or measured to be finished. `max_missions` survives only
as an explicit opt-in bound for a cautious caller; the default is unbounded, and
the suite pins that default.

**The completion signal comes free from the operator's own description:
"greyed out or not clickable".** An unclickable row leaves the screen unchanged
when clicked, so `start_row` clicks and asks whether anything happened:

| measured | reading |
|---|---|
| screen unchanged | the row is inert — **it is already done** |
| changed, no green check | something opened that cannot be started — done |
| changed, green check | started |

That is a POSITIVE reading of "finished", not the absence of a green check —
the negative-definition trap this file records four times over. Calibrated on
synthetic extremes: an identical screen 0.0000, a detail panel opening 0.3600,
a 4x4 flicker 0.0004; the gate is 0.02, which is 50x clear of the flicker.

`start_row` returns `(started, reason)` because "it did not start" was one
outcome covering two situations that need opposite responses: an **exhausted**
row is never revisited, a **transient** miss is left for the next sweep. And
the no-green-check frame is SAVED to `ref/auto/tp/`, because that is the shape
a completed TP row takes and one committed frame is all a proper template needs.

Termination is a measurement, so the count guard that remains
(`SWEEP_TRIPWIRE = 40`) is a **tripwire, not a policy** — if it ever fires the
termination check is broken, and it says so at error level rather than looking
like a tidy stop.

## "PRESSING RUN FREEZES FOR A BIT" — the sweep was first in line

`farm.in_mission` ran the command-bar geometry sweep BEFORE any template
check. A cold sweep on a 3440x1440 frame measures **12.96 s** against 1.28 s
warm, so starting a farm from the village paid thirteen seconds of 100% CPU in
`matchTemplate` to be told there is no command bar — on the one screen where
that was already obvious.

**And the panel freezes with it, which is why it reads as a hang rather than as
slowness.** Nothing captures during a sweep, and `Capture.on_activity` is what
pumps the operator's buttons, so Run/Stop are not even READ for the duration.
This file already records the same root cause for the 40-second dock freezes;
that fix budgeted the *repeats*, and this one removes the case from the common
path entirely.

Ordering by cost, with correctness preserved at each step:

| step | cost | what it proves |
|---|---|---|
| `IN_MISSION` anchors | ~0.07 s | positive: we ARE in a mission |
| `OUTSIDE_MISSION` anchors | ~0.07 s | positive: we are NOT |
| command-bar geometry | 12.96 s cold | only the genuinely ambiguous |

Measured cold on committed frames, before -> after:

    lobby (village)          12.96 s  ->  0.43 s     None
    lobby lb0                12.96 s  ->  0.43 s     None
    mission list all locked  12.96 s  ->  0.86 s     None
    combat, dark map         12.96 s  ->  1.94 s     command_bar
    battle between turns     12.96 s  ->  0.36 s     action_flag
    traversal map            13.42 s  ->  13.42 s    None   (see below)

**The sweep must stay for the ambiguous case.** It is the only thing that
catches a battle whose `action_flag` is occluded by an enemy sprite — measured
0.750 on the dark map, where the command buttons read 1.000. Rare, and it
should not be charged to every cycle in the village.

A TRAVERSAL frame still pays in full, and that is inherent: it is scenery, so
neither anchor set matches and there is nothing cheaper to ask. The cold-miss
budget bounds it — measured 13.50 s, then 1.97 s, 1.98 s, 1.98 s — so it is one
hit per process on that screen, not per cycle.

**`NOT_IN_MISSION` IS MISNAMED and cannot be reused for this.** Despite the
name it CONTAINS `action_flag`, `level_up`, `charge_btn` and `dodge_btn`,
because it is the veto list for `looks_like_mission_scene` ("this is not
walkable scenery"), not a statement about missions. Using it as the early-out
would report "not in a mission" in the middle of a battle — precisely the bug
`in_mission` exists to prevent. `OUTSIDE_MISSION` is therefore a strict subset
with every combat anchor removed, and the suite asserts both the subset
relation and that each combat anchor is absent from it.

## "IT SHOULD FARM UNTIL I PRESS STOP" — four paths said otherwise

`farm_missions` is a LOOPING task, but four separate code paths paused it
unconditionally on the first difficulty, and one of them was outright broken.

**THE BROKEN ONE: a SUCCESSFUL reconnect left the mode paused.**

    except Disconnected as e:
        self.mode = "paused"
        if not self.reconnect():
            break
        continue            # <- mode is still "paused"

So one transient CDP hiccup stopped a farm for good, and the only symptom was
a bot sitting idle until the operator noticed and pressed Run. A restored
connection is not a reason to stop working; the loop now remembers what it was
doing and resumes it, but only after the reconnect has actually succeeded.

**THE OTHER THREE were a missing distinction, not missing code.** An
unreadable screen, a task error and a mission-runner error each called
`self.mode = "paused"` directly. For a ONE-SHOT that is correct - it was asked
to do a thing once and the thing failed. For a loop it is wrong.

`Runner._setback(note, fatal=False)` makes it one decision instead of four,
and the question it asks is **whether a HUMAN is required** - not how alarming
the message looks:

| condition | response | why |
|---|---|---|
| logged out / `resume.HALT` | pause, always | only a person can sign in, and this bot must never try |
| unreadable screen | relog and carry on | that is the documented cure for the game's render stall |
| task / runner error | retry, bounded | may well be transient |
| a one-shot, anything | pause | its single job is over |

Bounded at `MAX_SETBACKS = 6` with a `3s * n` backoff capped at 15s, because
**a bot spinning silently is worse than one that stops and says why** - this
file's own rule, and the reason the guard was there in the first place. The
budget resets whenever the ladder reaches a screen it recognises, so it is
spent on the NEXT problem rather than staying exhausted.

### The game's render bug now gets recognised instead of walked through

The other half of "stuck" was not a pause at all. When the game draws the map
but never the CHARACTER SPRITE, no encounter can trigger and the mission is
unplayable - a defect in the game client, cured only by a reload and relog.

Left to the generic guards it cost about **150 s of visible wandering** (25
identical repeats at ~6 s per gate timeout) and filled the log with
`dead end (run 19); turning left`, which blames navigation for something
navigation cannot fix.

The signature is specific enough to act on, and separates cleanly on the
committed frames:

    render-stalled mission (run_00..run_10)   character found on  0 of 11
    healthy traversal maps (traverse_*)       character found on  4 of 4

So: traversal runs >= 8, ZERO battles, and no character on the map -> report
it as the GAME's bug by name, set `stats["render_stalled"]`, and return
STALLED at once. All three conditions are required together; runs alone, or an
absent character alone, is not the bug. Eight clears normal play comfortably -
a healthy mission reaches its first fight in a handful of runs, and the
recorded stalled run reached ten with none.

## SPEED: the frame was 2x bigger than the game, and negatives cost full price

Where the time actually went, measured rather than assumed. **Pagination was
never the problem** - page turns are 0.18-0.20 s, so all seven pages cost about
1.3 s. `cv2.matchTemplate` was: **73 ms for ONE template at ONE scale** on a
3440x1440 capture, so scoring all 59 cost 4.53 s and one resume-ladder step
cost ~1.7 s. Cost is linear in frame AREA, and this is the hottest path in the
bot - every subsystem goes through it.

Two independent fixes compound to **7.55x**, verified over **6018
template/frame comparisons with ZERO decision changes and ZERO coordinate
changes**.

**1. SEARCH ONLY THE GAME.** The canvas is 1920 px of a 3440 px capture; the
rest is desktop wallpaper and the bot's own panel. Cropping does not merely
preserve accuracy, it IMPROVES it - there is less unrelated art to match by
accident:

    lobby_rail_fortune (positive)  0.976 -> 0.976   identical
    char_slot_level    (negative)  0.512 -> 0.443   MORE margin
    page_next          (negative)  0.548 -> 0.531   MORE margin

`Capture.apply_search_band` sets it from the LIVE rect on every full frame, not
per cycle - a mission blocks for minutes, and a band describing where the game
*used to be* would lose every anchor at once. If it cannot be measured the band
is CLEARED: searching the whole frame is merely slower, while searching the
wrong strip is wrong. That required distinguishing "measured, and it is at the
reference" from "could not measure", which `game_metrics` could not do - both
returned `(0, 0, 1.0)`. Hence `game_metrics_ok()`.

**PAD THE BAND BY THE TEMPLATE'S OWN WIDTH.** `matchTemplate` requires the
template to fit ENTIRELY inside the searched region, so a match straddling the
edge is annihilated, not degraded. `lobby_logo` is 216 px wide and matches with
its centre at x=862 - inside a band starting at 760 - but its LEFT EDGE sits at
**754, six pixels outside**. The bare band took it from **0.998 to 0.299**, and
21 such mismatches appeared across the committed frames. Six pixels, and the
lobby stopped being recognised.

**2. REJECT NEGATIVES AT HALF RESOLUTION.** A negative costs exactly what a
positive costs, and nearly everything scored is a negative - "no anchor
matched" is both the common case and the expensive one. So score at half scale
first and only pay full price for candidates that could still clear threshold.
Halving squeezes margin from BOTH ends (positives fall, negatives rise), so the
cheap gate sits `COARSE_RELAX` below the real threshold. The value is measured:

    relax   missed positives   worst headroom   negatives rejected
    0.10           1               -0.007            98.6%
    0.18           0               +0.073            96.1%
    0.30           0               +0.193            90.2%

0.10 already loses a real match. 0.18 is the smallest with zero misses AND
headroom above the 0.07 that thresholds are calibrated to. The tightest
positive is `nav_jutsu`. Masked templates SKIP the prefilter - they score with
`TM_CCORR_NORMED`, whose values are not comparable, and none of the current 59
carry alpha, so the table above was never verified for them.

The halved FRAME is cached by **weak reference**, not by `id()`: an id is
recycled once an array is freed, which would silently hand back another
frame's pixels. One sweep over 59 templates then resizes once, not 59 times.

**The geometry sweep needed the band separately.** `geometry._best` calls
`matchTemplate` directly and so saw none of this, while being the most
expensive thing left: a cold 90-scale sweep on a traversal frame measured
12.86 s. Banding it brings that to **7.97 s**, with the command-bar anchor
landing identically ((1670, 977) at scale 1.0 both ways).

### CORRECTION: capping OpenCV's threads does NOTHING on this build

`cv2` defaults to one thread per core (18 here) and every match saturates them,
which is most of "why does my Mac get hot". Capping looked like free relief,
and a thread sweep came back suspiciously flat - 1.265 s at "1 thread" against
1.281 s at "18".

**That sweep was measuring the same configuration seven times.** This wheel is
built with GCD as its parallel framework (`cv2.getBuildInformation()` ->
"Parallel framework: GCD"), and under GCD **`setNumThreads` is a no-op**:

    setNumThreads(4) -> getNumThreads() == 18
    setNumThreads(1) -> getNumThreads() == 18

So the flat curve says nothing whatever about threading. OpenCV's thread count
is simply **not controllable from Python on this build**, and the "threading
buys nothing" conclusion drawn from it was unsupported.

The call is kept because it is not inert everywhere - Linux and Windows wheels
use TBB/pthreads/OpenMP - and it now logs which case applies. But on this host
**the only thing that reduces the heat is doing less work**, which the 7.55x
above actually does.

General lesson, and it is the same one this file keeps recording: before
trusting a measurement, check that the knob you turned is connected to
anything. A suspiciously flat curve is usually a disconnected knob, not a
discovery.

### The log had no timestamps at all

`run/app.log` recorded a whole session with no times in it - the stamps an
operator sees pasting from the dock's pane come from the panel, and the file
never had them. So every timing conclusion had to come from durations the code
happened to print itself, and "why did that take so long" was unanswerable from
the record. `Log._emit` stamps the stream too, and distinguishes levels, since
`warning = error = info` made a crash and a routine step look identical.

## COOLDOWNS: learn the LENGTH, do not read the icon

The operator's report was that skills on cooldown still get clicked, the click
does nothing, and the bot waits before falling through to Attack. Both halves
of the fix are in; one is live, one is dormant awaiting a single frame.

**READING THE ICON IS THE WRONG TOOL, and this file already measured why.**
Slot saturation ran **56.2 .. 190.8 continuously with no bimodal split**, and a
pale-pink slot reads 56 while perfectly usable. So there is no threshold, and
`SlotBaseline` stays a cross-check only.

**THE LENGTHS WERE ALWAYS TRACKABLE, AND NOBODY EVER TRACKED THEM.** The game's
own battle processor decrements cooldowns once per round (`nextRound()` ->
`reduceSkillCooldown(1)`), so a cooldown is an exact small integer, and
`CooldownTracker` has existed for that all along. But the lengths live in a
server-fed `SKILL_DATA`, so the config said to measure each slot by hand -
which never happened for a single one. `rounds_per_slot` is still `{}`, so
`ready()` always returned None and the rotation always fell back to
rotate-on-resolve: click a cooling skill, wait out ~6 s, try the next thing.

**So bracket it from outcomes already recorded** - no new perception at all:

    used at U, fired again at R   ->  cooldown <= R - U
    used at U, FAILED at R        ->  cooldown >  R - U

Squeeze until they meet. Same shape as the kekkai Mastermind: keep everything
consistent with the evidence and wait for one survivor. Simulated against true
lengths of 1, 2, 3, 5 and 7, it converges after **one full cycle** (a 3-round
cooldown is known by round 4). Once known, the slot is WITHHELD instead of
clicked - strictly better than reacting to a refusal, because the wasted click
never happens.

**A COOLDOWN IS AT LEAST ONE ROUND, and that bound is not optional.** Without
it a 1-round cooldown can never be learned: it is ready again the next round,
so it never fails, so no failure-derived bound is ever recorded and the bracket
sits at `(None, 1)` for ever. Measured exactly that way - 2, 3, 5 and 7 all
converged while 1 stayed unknown.

**THE FAILURE SIDE IS THE DANGEROUS HALF.** A skill can fail because it is
cooling, because we are stunned, because CP ran out, or because the click
missed - and only the first says anything about a cooldown. A lower bound
invented from a stun would **disable a working skill**, which is far worse than
learning nothing. So a failure is admitted only when the turn was otherwise
healthy, which means *something else resolved* - and that is not known until
the turn is over. Failures are therefore collected and flushed by
`SkillRotation.turn_ended(something_resolved)`:

    a skill resolved            -> healthy, failures are usable evidence
    the closing attack resolved -> healthy (an attack landing rules out a stun)
    only Dodge resolved         -> STUN, so the turn teaches nothing
    nothing resolved            -> teaches nothing

Every exit from `_take_action` flushes, because failures left pending would be
attributed to the NEXT turn's round number - a silent off-by-one in the one
place a wrong number disables a good skill. A bracket that closes on something
implausible (> `MAX_PLAUSIBLE`) is thrown away rather than acted on.

**AN API WHERE THE CALL ORDER FAILS SILENTLY IS A BAD API.** The first version
had `observe_success` / `observe_failure` beside `use()`, and `use()` overwrote
the last-used round - so calling it first made the gap zero and the upper bound
was never taken. A simulated 3-round cooldown ran eleven rounds and learned
nothing, bracket stuck at `(3, None)`. Now `record(slot, fired, healthy)` is
the single entry point and does the bookkeeping where the order cannot be got
wrong.

### The refusal message: implemented, dormant, and waiting for one frame

When a skill is on cooldown the game says so at the top of the screen and
nothing else happens - so waiting for the command bar to vanish means waiting
out the full `action_timeout` (~6 s) to learn what the game said immediately.
Two failures in a turn is 12 s of standing still.

`_wait_resolved` now watches for that refusal ALONGSIDE the success conditions,
losing the race deliberately: whichever fires first ends the wait, and a
refusal returns not-resolved at once.

**It self-activates when `tpl/skill_cooldown.png` exists, and is skipped until
then.** The template has to be cut from a real frame, and nobody has looked at
that message yet - inventing a crop for it would be exactly the eyeball mistake
this file keeps paying for. So `_keep_failed_frame` saves the screen that
refused us (bounded to 3 per process, and it stops entirely once the template
exists), which is the same trick that eventually solved the mission list, the
between-turns battle, the seal-broken dialog and the Level Up panel: the hard
part was always CATCHING the frame.

## STOP RESTARTS ITSELF — a reset that costs a terminal trip is a chore

Stop means START AGAIN FROM NOTHING here, and killing the process is most of
that reset for free: the cycle counter, the unknown streak, the relog budget,
cached battle geometry and remembered dud targets all live in memory and die
with it.

But **the panel is injected into the PAGE, so it outlives the process it was
talking to.** The operator was left with a live-looking panel whose buttons had
no receiver - it read "no bot attached", printed a relaunch command, and every
single Stop meant going to a terminal. This file already recorded that as the
reason Stop was once changed back to a cooperative abort; the real fix is for
Stop to bring the bot back itself.

`Runner._respawn` launches the replacement BEFORE exiting, and **attaches** to
the browser that is already running. Attaching is not a detail: the game's
session cookie is a browser-session cookie, so relaunching Chrome would cost a
manual sign-in that only a human can perform.

**THE ORDER IS THE WHOLE TRICK**, and getting it wrong means two bots clicking
one game:

    1. release the pid lock - or the replacement is refused by the very guard
       that exists to prevent the duplicate we are about to create
    2. hand the child OUR pid, so it waits for us to be gone
    3. exit

Note the parent performs NO game interaction between releasing the lock and
exiting - it spawns, prints and goes - so the child's wait is defence in depth
rather than the thing that makes this safe.

### `os.kill(pid, 0)` IS NOT A LIVENESS TEST — a zombie passes it

The child's wait was first written as a plain `os.kill(pid, 0)` poll, and a
test caught it immediately: **a helper that lived three seconds was still
reported alive twenty seconds later.** A finished pid lingers in the process
table until its parent reaps it, and `kill(0)` succeeds against a zombie quite
happily.

This is the same trap this file already records for the pid lock - *a bare pid
proves something exists, never that it is alive and ours* - reached from a
different direction. `_dead()` therefore asks the OS for the process STATE and
treats `Z` as gone. Measured after the fix: 3.1 s instead of 20.2 s.

`--wait-for-pid` is `argparse.SUPPRESS`ed. It is set by Stop's own relaunch and
is not something a human should be typing.

**Stop and Quit must stay different**, and the suite asserts it: Stop relaunches
attached, Quit removes the panel and leaves for good.

## THE "Go!" BADGE IS NOT AN ENEMY — and it is the heading

A live infinite loop, and the bot was trying to walk to a piece of UI.

The game draws an **animated orange "Go!" badge** pointing the way on. It
animates, so `find_moving_figure` calls it alive - *"something MOVED at
(2472, 522) - that is alive"* - and it sits above `FIG_MIN_Y`, so the
walkable-ground filter passes it too. But it is painted OVER the scene:
clicking it does nothing at all.

Combined with the exemption that a moving target never joins the dud list ("a
thing that MOVES is alive"), that produced seven identical runs - engage the
badge, time out, run to the WRONG edge, come back - on a map with **no enemy on
it whatsoever**. The tell was in the log and was the one this file already
records for static objects: *byte-identical coordinates every pass*, both for
the character (2220, 704) and the "enemy" (2472, 522).

**This is the confirmation the LEAD note was waiting for.** That note kept
`tpl/_lead_go_arrow.png` behind a leading underscore (so `load_templates`
skips it) and said: *"Before using it: confirm it appears on several NORMAL
traversal maps."* Measured on the stuck frame against every traversal frame
held:

    the stuck frame            0.845   (centre 2531, 497)
    eight other traversal      0.309
                               ------
    margin                     0.536

and its centre is 60 px from the phantom enemy - the same object. It is now
`tpl/go_arrow.png`, registered at threshold 0.75.

**Two things fall out of one template.**

1. **It is vetoed as a unit.** A candidate within `ARROW_RADIUS` of the badge is
   the badge. This is the same shape of fix this file keeps arriving at: a
   detector defined by "something changed" needs something that says *yes, but
   not that*.

2. **It names the heading, and it beats the spawn rule.** The spawn heuristic
   ("you came in through an edge, so head away from it") is a good guess and
   was wrong here: the character stood at x=2220, right of the 1720 centre, so
   it said *left* and the runner ran left into a dead end seven times - while
   the badge sat to its RIGHT at x=2531.

   Read by **position, not by the glyph's direction**: walk toward where the
   badge IS. That needs no assumption about whether a leftward map mirrors the
   arrow - still unknown - and it degrades to the spawn rule when absent.

### "A thing that moves is alive" was too absolute

Both extremes were measured, and both cost a real failure:

    dud a mover on its FIRST failure  -> retired the only real enemy on the map
                                         after one 6 s timeout
    never dud a mover                 -> spun on the Go! badge for ever

So `MOVING_DUD_AFTER = 3`: enough attempts for a walk that genuinely needs
longer, after which a thing that moves but stays unreachable is accepted as
animated scenery. Torches, water and flags would have done the same eventually;
the badge just got there first.

### A guard that fires on correct code gets deleted

The attribute-name check added after the `self.cap` / `self.capture` bug
asserted that every `getattr(self, X, None)` names something assigned FROM the
capture. That was true when written and is not an invariant - `_mover_fails` is
read the same way and has nothing to do with capture - so it failed on correct
code the moment this change landed.

The real invariant is narrower and more useful: **a name read off `self` must
be a name set on `self`.** Verified that it still catches the original bug -
reintroducing the typo reports `never-assigned: ['cap']` - while passing on
every legitimate lookup.

## A TEST THAT READS CODE CANNOT CATCH CODE THAT DOES NOT RUN

The Go! badge work shipped with **749 checks passing** and `_traverse` crashing
on its very first real call:

    ERROR mission runner error: UnboundLocalError: local variable 'arrow'
                                referenced before assignment

The badge answers two separate questions - *is that moving thing a unit?* and
*which way do we walk?* - and the second is asked FIRST in the function. The
assignment went in next to the enemy search, below both, so it was referenced
before it existed. Every mission died the instant it reached the map, the farm
caught it, and the bot relogged and tried again.

**The tests could not have caught it.** They asserted source-level properties
(`"arrow[0] > pos[0]" in src`, ordering of two `find` offsets) and exercised
`find_go_arrow` and `find_character` individually - all of which passed against
a function that could not execute. `test_traverse_actually_runs_on_a_real_frame`
now wires up fakes for the capture, actor and gate and CALLS it, asserting it
does not raise, that it walks toward the badge, and that it never clicks the
badge itself. Source inspection is a supplement to execution, never a
substitute.

### ASSIGNING A NAME ANYWHERE MAKES IT LOCAL EVERYWHERE — the second UnboundLocalError

Shipped with 998 checks passing, and it died on the first real mission of the
day:

    SS: this mission is rune
    ERROR task error: UnboundLocalError: local variable 'play' referenced
                      before assignment

`run_one` dispatches four ways. The puzzle branch had been written as

    play = play_balance if kind == "balance" else play_lights

and Python makes a name local to the WHOLE function if it is assigned
anywhere in it — so the RUNE branch's call to the module-level `play` resolved
to an unbound local and raised before doing anything at all. The rune family
is the one this project has supported longest, and it was the one that broke.

**This is the second instance of exactly this bug.** `_traverse` shipped with
`UnboundLocalError: local variable 'arrow'` behind 749 passing checks, and
this file already draws the conclusion: *a test that reads code cannot catch
code that does not run*. Every assertion written about `run_one` inspected its
text — its branches, its ordering, which functions it mentions — and the text
was never the problem.

`test_every_run_one_branch_actually_runs` CALLS it once per family with fakes,
and additionally asserts from the BYTECODE that `play` is not a local of
`run_one` at all, which does not depend on how the line happens to be spelled.
Verified by reintroducing the bug: the test reports the identical
`UnboundLocalError` the live run hit.

General rule, now paid for twice: **a dispatcher must be executed down every
branch it offers.** Fakes are cheap; the branch that never runs in testing is
the branch that runs in production.

## "THE TASK RETURNED" IS NOT "THE TASK ACHIEVED SOMETHING"

The same live loop exposed a hole in the setback budget. `farm.farm` CATCHES a
mission-runner crash, logs it and breaks - so the task returned normally,
having banked nothing, and `step()` cleared the budget on every cycle. Measured
over one session: **13 relogs, 8 "SAME failure" warnings, 1 banked mission**,
with every line reading `setback 1/6` and never counting past one.

This is the second time this budget has been defeated by resetting it on the
wrong signal. First it reset on reaching the lobby, which a relog always
achieves. Now it reset on a task returning, which a swallowed crash also
achieves. The measure has to be the thing actually wanted:

    farm  ->  banked > 0        starting a mission is not completing one

A task that reports nothing is assumed to have made progress, so nothing else
changes behaviour; `_progress` defaults to True before each run.

**General shape, and it has now bitten three times:** a guard bounded by a
counter is only as good as the event that clears the counter. Ask what the
counter is protecting against, then clear it on evidence that the protection is
no longer needed - never on something merely correlated with progress.

## DOUBLE-CLICK LAUNCHERS — the failure modes are all in the shell, not Python

`Start NS Bot.command` (macOS), `Start NS Bot.bat` (Windows) and
`start-ns-bot.sh` + `NS Bot.desktop` (Linux). No build step and no packaging:
the point is that someone who will not open a terminal can start the bot, and a
readable script does that as well as a binary would while staying fixable by
whoever has to fix it.

Four things each launcher has to get right, and every one of them is a way a
launcher fails silently:

**THE WINDOW MUST NOT VANISH.** A double-clicked script that fails and closes
instantly is worse than no launcher: there is nothing to read, and no way to
tell a crash from a clean exit. The shells `trap ... EXIT` and the batch file
routes every failure through a `pause`.

**NEVER ASSUME THE WORKING DIRECTORY.** Finder and Explorer start scripts from
somewhere arbitrary, so each one cd's to its own location first (`dirname
$BASH_SOURCE`, `%~dp0`, `readlink -f` on Linux for the symlink case).

**APPEND TO THE LOG, NEVER TRUNCATE.** `tee run/app.log` would wipe a RUNNING
instance's log on a second double-click, before `app.py` even got as far as
refusing to start - destroying the record of the session actually doing the
work. Verified live: with an instance running, the launcher refused cleanly and
the log grew (3200 -> 3201 lines) instead of being emptied.

**BATCH PARSES A WHOLE PARENTHESISED BLOCK BEFORE RUNNING IT**, so `if
errorlevel` inside one reads its value from BEFORE the block, and a batch file
written with `||` blocks silently ignores its own failures. The `.bat` uses
plain `if errorlevel` plus `goto` throughout, and re-checks the thing it wanted
(`does .venv\\Scripts\\python.exe exist now?`, `does cv2 import now?`) rather
than trusting an exit code it may have mis-read.

No launcher passes `--attach`. `browser.launch(reuse=True)` already attaches to
a browser serving CDP and starts one only when nothing is, so guessing is not
needed - and guessing wrong is how an operator ends up staring at "no page
target after 40s".

The Linux `.desktop` needs an absolute `Exec` path, because a .desktop file is
not run from the folder it lives in. `start-ns-bot.sh` substitutes it on first
run: telling someone who is "not comfortable using a script" to hand-edit a
config file hands back the problem the launcher exists to remove.

## ANY CHROMIUM BROWSER WILL DO — the requirement is CDP, not Chrome

"Not everyone has Chrome" turned out to be a smaller problem than it sounds,
because nothing in this project is Chrome-specific. `--remote-debugging-port`,
`--app=`, `Input.dispatchMouseEvent`, `Runtime.addBinding` and
`Page.addScriptToEvaluateOnNewDocument` are all **Chromium** features, and CDP
is Chromium's own protocol - so every fork speaks it. Supporting Edge or Brave
was a matter of finding the binary, not of writing code.

**Verified live against Microsoft Edge (Edg/152)** through this project's own
`browser.launch()`, exercising exactly the operations the bot depends on:

    Runtime.evaluate                       -> 2
    Emulation.setDeviceMetricsOverride     -> [1720, 720, 2]
    Page.captureScreenshot                 -> 61166 bytes
    Page.addScriptToEvaluateOnNewDocument  -> accepted
    Runtime.addBinding                     -> function
    Input.dispatchMouseEvent               -> accepted

So the detection list covers Chrome, Chromium, Edge, Brave, Vivaldi, Opera and
Arc, on all three platforms, including **per-user install locations** - a
machine without admin rights has its browser under the user's own directory,
not Program Files - and snap/flatpak paths on Linux. `--browser` overrides it
(the error message had promised that flag for a while before it existed), a
config `target.browser` sits behind that, and the log names the browser it
actually used, which is unguessable from outside once several are installed.

**FIREFOX AND SAFARI CANNOT BE ADDED BY PUTTING A PATH IN THE LIST**, and the
not-found message says so, because otherwise someone will try:

| | protocol |
|---|---|
| Chromium forks | CDP - what this bot speaks |
| Firefox | WebDriver BiDi; its CDP shim was always partial and is being removed |
| Safari | WebKit Inspector Protocol, driven through `safaridriver` |

Either needs a second transport for capture, input and script injection - a
rewrite of `cdp.py`, `act.py` and `capture.py`.

### A relative candidate path must never be accepted

Several Windows entries are built from `%LOCALAPPDATA%`, and when that is unset
`os.path.join("", ...)` yields a RELATIVE path - which `os.path.exists` then
resolves against the current working directory. The launchers cd into the bot's
own folder, so a stray `Google\Chrome\Application\chrome.exe` sitting there
would have been picked up as an installed browser. `find_browser` requires
`os.path.isabs`.

That gap surfaced from a test that FAILED AGAINST CORRECT CODE: it asserted the
per-user paths by inspecting the evaluated candidate strings, and on macOS
`%LOCALAPPDATA%` is empty, so the intent had already collapsed to a bare
relative string and was invisible. The assertion now reads the module source
instead - the right move whenever a platform-specific value cannot exist on the
platform running the test.

### A FAILED LOG REDIRECT MUST NOT STOP THE RELAUNCH

From Windows, pressing Stop:

    could not relaunch: [Errno 13] Permission denied: '...\run/app.log'
    stopped by the operator - relaunch failed

which is the dead-panel state Stop exists to prevent - the panel lives in the
PAGE, survives the process, and is left with no receiver. The launcher
redirects with cmd's `>> run\app.log`, and **cmd opens that file without
sharing writes**, so the child's open for append is refused. POSIX allows the
same open, which is why it never showed up here.

The bug is in the error handling, not the file: where the child's output goes
is a convenience, whether the child STARTS is the point. `_child_output`
degrades - the shared log, then a private `app-<pid>.log`, then `DEVNULL` -
and never raises out of the relaunch. The test executes that chain with every
path denied rather than reading it.

### `os.kill(pid, 0)` IS A KILL ON WINDOWS, NOT A PROBE

Reported from a Windows machine: *"when I clicked stop it did not immediately
reconnect the panel and just died."* That is precisely what the code did.

CPython's `os.kill` only sends a real signal for `CTRL_C_EVENT` and
`CTRL_BREAK_EVENT`; for every other value it calls
`TerminateProcess(handle, sig)`. So the POSIX liveness idiom
`os.kill(pid, 0)` **terminates the target with exit code 0** there — and
`_respawn` releases the pid lock by asking `_lock_holder(lock) == os.getpid()`,
which probed OUR OWN pid. Pressing Stop therefore made the process kill itself
before it could spawn its replacement, leaving the injected panel with no
receiver: exactly the "dead panel, no bot" state Stop was rewritten to prevent.

`_alive(pid)` is now the single liveness probe and is non-destructive on both
platforms. Windows has no zombies, so a handle that signals means the process
is finished, and `WaitForSingleObject(h, 0)` answers directly —
`WAIT_TIMEOUT` running, `WAIT_OBJECT_0` exited — opened with `SYNCHRONIZE`,
the least right that permits the wait, so it cannot modify the target even by
accident.

**Two more POSIX-only assumptions sat in the same path, and both failed
SILENTLY rather than loudly:**

    `_dead`      shelled out to `ps`, which raises FileNotFoundError on
                 Windows, was swallowed, and returned "not dead" for ever - so
                 every `_wait_for_exit` burned its full 10 s timeout
    `_proc_cmd`  also shelled out to `ps`, returning "" for every pid - and
                 `_lock_holder` reads an unreadable command line as "not the
                 holder we recorded" and DROPS THE LOCK. The one guard against
                 two bots clicking one game was inert on Windows, on every
                 launch.

`_proc_cmd` now uses `Get-CimInstance Win32_Process` with a `wmic` fallback
(`wmic` is deprecated and absent on recent Windows, so it cannot be the only
route), and `_dead` no longer reaches for `ps` there at all.

**The unknown-answer direction matters and is chosen deliberately.** When
`_alive` genuinely cannot tell, it says ALIVE: a false "alive" costs a refused
launch the operator can resolve by killing a process, while a false "dead"
lets two instances click the same game — which this file records happening
eight instances over.

**A test that only runs on POSIX still catches this**, which is the point:
it asserts there is exactly ONE `os.kill` call site, that it is inside
`_alive`, and that the platform test precedes it in the CODE (the first
version of that assertion matched the docstring's own prose and failed on
correct code — the trap this suite keeps re-learning). It also spawns a real
child, probes it four times and asserts it is still running, which the old
code would have killed.

### AND TWO WINDOWS REPORTS THAT WERE NOT BUGS AT ALL

The same machine reported the window still scrollable and webgl not working
for the bot. Both features exist ONLY in commit `75053db`, which sat
**unpushed** while that machine was tested; the newest commit it could have
pulled predates the scroll lock and `tpl/webgl/` entirely. Before diagnosing a
platform difference, check `git log --oneline -1` on the platform — a missing
commit and a broken port look identical from the outside.

## THE "Go!" BADGE BLINKS — which is why one veto worked and the next did not

The badge veto shipped and then failed intermittently. Nine seconds apart in
one live log, on the same pixel:

    10:55:44  the thing moving at (2472, 522) is the game's own Go! badge at
              (2531, 497), not a unit - ignoring it
    10:55:53  something MOVED at (2472, 522) - that is alive

Measured across seven frames 0.35 s apart on one map: **0.845 on two of them
and 0.326 on the other five.** A spread of 0.519 is far too large for animation
jitter - at 0.326 the badge is **not drawn at all**. It blinks, and the template
therefore matches on roughly two frames in seven.

**And that is exactly why the movement detector locks onto it so reliably.** A
thing that appears and vanishes outright is a stronger frame-to-frame signal
than any walking sprite. So the badge is seen as alive on nearly every pass and
recognised as furniture on barely a third of them - the worst possible
combination, and one no amount of threshold tuning fixes, because on a blink-off
frame there is nothing to match.

**A badge does not move within a map, so the last sighting is remembered** and
used on the frames where it is not drawn. Replaying the real blink sequence: the
veto now holds on 6 of 6 frames where it previously held on 1. `forget_go_arrow`
clears it on a map transition, so the memory cannot veto a genuine enemy that
happens to stand where the previous map's badge was.

**A general note on template thresholds.** A single positive sample tells you
nothing about a template's VARIANCE. `go_arrow` was calibrated from one frame at
0.845 with a worst negative of 0.309, which looked like an enormous margin - and
the true distribution turned out to be bimodal, with most frames at 0.326.
Prefer several frames spread over time before trusting a threshold, especially
for anything the game animates.

### Arriving at the badge must not reverse the heading

Second fault in the same log. "Walk toward the badge" has nothing useful to say
once you are standing on it, and saying it anyway sends you backwards:

    ran right to the map edge at x=2629
    badge at x=2531 is now on our LEFT
    -> heading reversed, ran all the way back to x=789

The badge marks the way ON, not a destination to stand on. Within
`ARROW_REACHED` (260 px, against the 71 px gap that caused the reversal) the
heading is KEPT rather than recomputed. Continuing right is what produced
`moved on` about thirty-five seconds later, so the guard would have saved that
whole excursion.

## THE EXAM RUNE PUZZLE — already supported; only the navigation is missing

The reference bot solved this puzzle for the **Jounin and Sage exams** and never
for TP; we did the exact opposite. Porting "the exams" turned out to need no new
solver at all, because every piece of the path was already context-free:

* `minigame.classify` reads the family OFF THE SCREEN rather than from a
  configured label - the decision recorded above as "the pixels win" - so an
  exam seal is recognised as a kekkai with nothing added;
* `kekkai_play` contains no TP assumptions (asserted by the suite, which greps
  it for `tp_training_row`, `special_tab` and `TP Training`);
* `hunt_and_solve` **counts the seal's nodes** and uses that as the code length,
  which is the whole of the difference between an exam and TP - their bot keyed
  four coordinate tables 2..5, and TP only ever showed 3 and 5;
* `kekkai.candidates` is a plain product over the six runes, so it is generic in
  the length.

Verified, since lengths **2 and 4 had never been exercised on this side**:

    length   candidates   secrets tried   solved   avg guesses   worst
       2             36        36 (all)       36          3.72       6
       3            216       216 (all)      216          4.07       6
       4          1,296              80       80          5.28       7
       5          7,776              80       80          6.06       8

**WHAT IS NOT PORTED IS THE NAVIGATION**, and it cannot be: nobody has captured
an exam's screens, and inventing templates for a menu nobody has looked at is
the eyeball mistake this file exists to prevent. Their coordinate tables are for
their own geometry on a different private-server build, so they would not
transfer even if kept.

So `tasks.ExamKekkai` divides the labour honestly. `needs_lobby` is False - for
the same reason the farm can start mid-mission, since the resume ladder
deliberately cannot name an exam screen and demanding the lobby first would make
the task unusable. The operator navigates to the exam and presses Run; the bot
plays the puzzle. When it finds no puzzle it says so precisely and **saves the
frame**, which is how the navigation gets taught - the same trick that
eventually solved the mission list, the between-turns battle, the seal-broken
dialog and the Level Up panel.

## SS TRAINING — the multi-stage rune puzzle, cleared

Mission Room -> `Special` tab -> `SS Training`, one row below TP Training at
(2118, 1037). Five missions, all Lv 80, XP 10,000, Gold 10,000, flame 30 -
five times TP's reward against TP's flame of 10:

    Twins Unicorn · Sage Power Seal · Forest Guardians
    Balance Control · Sage Sealed Boxes

`Sage Power Seal` is **the same rune Mastermind as the TP kekkai**, and it has
been cleared end to end: two stages, `Mission Success!` banked.

**THE GAME STATES ITS OWN FEEDBACK MAPPING.** A rules panel opens before the
puzzle and says outright that the green disc counts runes "correct in both
pattern and position" and the gold disc "a correct rune pattern placed in the
wrong position". That is exactly what this project established by carrying both
hypotheses through live play - the mapping was right, and it is now confirmed
from the game rather than inferred.

### One mission is several stages, and the length ESCALATES

    stage 1   FIVE nodes  -> length 5, answer Red,White,Blue,Black,Green
    stage 2   SIX  nodes  -> length 6, answer Yellow,Red,Black,Green,Blue,White

So the length is re-read PER STAGE from the seal's node count and never
carried over. `count_nodes` needs a bigger box than the TP triangle uses -
measured, the default (260, 160) clips a pentagon and returns 3 for a five-node
seal, so `ss.NODE_BOX` is (320, 300).

**CORRECTION — THE SECRET LOOKS LIKE A PERMUTATION.** This section used to
say "repeats occur", on the strength of a five-node stage whose two SURVIVORS
both repeated White. That was an inference from a model, not an observed
answer, and it was made during the run where a misread counter poisoned the
model — which is precisely what leaves only repeat-y survivors, by eliminating
the true repeat-free code.

Every answer the bot has actually CONFIRMED is repeat-free:

    len 3   White, Blue, Yellow
    len 3   Green, Blue, Black
    len 5   Yellow, Blue, Green, Black, Red
    len 6   Yellow, Black, Red, Green, Blue, White   <- all six, once each

The operator found this by watching the bot click the same rune three times
and asking whether the puzzle allows that at all. It is a good question and
the answer changes the search space by 65x at length 6 — 720 codes instead of
46,656 — which matters because ten rows is the whole budget and running out
fails the MISSION, not the stage.

**`kekkai.AUTO` acts on it without gambling on it**, because an empty pool is
already a detected condition here: `next_guess` returns None and the caller
stops rather than guessing. So a secret that DOES repeat cannot produce a
wrong answer — only an exhausted permutation pool, after which AUTO widens to
the full space carrying the same history. `surviving()` returns which space
was used, so a widen is visible rather than silent, and the evidence keeps
accumulating.

Measured over random secrets:

    permutation secrets   len 3  40/40  worst 5
                          len 5  40/40  worst 6      (was avg 6.06, worst 8)
                          len 6  40/40  worst 7
    REPEATING secrets     len 3  30/30      len 5  30/30   - still solved

### TEN ROWS PER STAGE, AND RUNNING OUT FAILS THE MISSION

Not the stage - the whole mission, with `Mission Fail - Sorry, try again next
time`. That was learned expensively: a diagnostic sweep of six all-same probes
left four rows for solving and lost the mission. Knuth selection needs roughly
six to eight guesses at length six, which fits ten; a probe sweep does not.

An all-same guess is still the one probe whose answer is knowable without
reading it - gold MUST be 0, because no wrong-position match is possible when
every slot holds the same rune - which makes it perfect for harvesting and far
too expensive for playing.

### THE STAGE DIALOGS ARE TOLD APART BY COLOUR

    Stage Clear    a GREEN button, area 31542, 351x108, centre (1712,  991)
    Mission Fail   a RED   button, area 29891, 352x108, centre (1720, 1015)

Same shape, same place; only the colour differs, and it decides whether to
carry on to the next stage or stop. Checking the dialog BEFORE the puzzle
matters: a solved stage leaves the panel closed and the dialog up, and "the
panel is gone" alone cannot tell a clear from a failed mission - a script that
read a closed panel as "solved" reported a win on a mission that had just
failed.

Verified against a real distractor: the game's own "Learn new Jutsu" nag has a
green `Go to Academy` button inside the same band and is correctly NOT read as
a stage dialog, because its aspect ratio is far wider than an OK button's 3.25.

### GEOMETRY THAT WAS HARDCODED TO THE TP LAYOUT

Five things, all now located rather than assumed, and each validated against
both layouts:

| | was | now |
|---|---|---|
| history column x | fixed 1950..2030, found NOTHING on SS | TP 1987, SS 2058 |
| gold disc offset | fixed 86 | TP 84..86, SS 78..79 |
| `count_filled` window | fixed, landed on SS's gold discs | relative to the located gold column |
| `find_rune_buttons` | first row of six wins | the row with the widest HUE SPREAD |
| `find_confirm_point` | largest dark blob | a SOLID disc, not a dark ring |

**The rune row was the worst of them.** At length six there are six numbered
slots AND six rune discs - two rows of six evenly spaced identical circles -
and it locked onto the SLOTS. Every click landed on an empty numbered slot and
six successive guesses read back byte-identical feedback from an untouched
board. Saturation does not separate them (mean S 75 against 85); hue spread
does, decisively:

    slot row   H = 19 19 19 19 19 19        spread   0
    rune row   H = 53 174 120 0 24 15       spread 174

`find_confirm_point` had two failures in one. It picked a dark RING - a node's
outline - whose centroid lands in the pale middle, so it returned a point that
was BRIGHT (grey 220) out of a mask built from `g < 90`. Requiring a solid fill
fixes it: the real submit disc measures fill 0.65 / 0.66 / 0.77 across the
three layouts while every distractor sits at 0.18..0.47. And a "the centre must
be dark" test was tried and REJECTED THE RIGHT BLOB everywhere, because the
kanji is drawn LIGHT on the dark disc - centre grey 148 on both SS stages and
204 on TP.

### `count_filled` BY EDGES, because a black rune is not saturated

The old version counted a row as filled from the fraction of strongly
saturated pixels in the rune strip. That works until a row contains the BLACK
rune: measured on a scroll with eight rows filled, the two rows holding black
read 0.09 against a 0.12 gate and were counted EMPTY, so it returned 6 for 8 -
and the solver then read stale rows and converged by luck.

Hue is irrelevant to "is there an icon here, or a dash?", so measure STRUCTURE.
Edge density separates cleanly and a black rune has as strong an outline as a
bright one:

    filled rows   0.160 .. 0.215 (SS, including the black-rune rows), 0.168 (TP)
    empty rows    0.009 .. 0.089 (both layouts, both renderers)

### `solve_live` CAN RESUME, because rows are the scarce resource

It used to start a fresh model on every call, so a restart replayed the same
openers into fresh rows - and a restart is exactly what happens after an
unreadable digit is harvested and labelled. At length five the solver needs
about six of the ten rows, so two wasted restarts lose the mission.

`history` seeds it with answers already on the scroll and `on_history` reports
them back after every answer, so a caller can resume. **The seed shape is the
flat `(guess, green, gold)` this function already appends** - a first attempt
passed a nested `(guess, (green, gold))` and would have filtered against a
shape the solver never produces.

Measured working: stage 1 resumed from two answers and solved in four more;
stage 2 resumed three times across digit harvests and still finished inside its
ten rows.

### The digit exemplars are complete, 0 to 6

Harvested during the clear, both discs. The set was the whole reason the solver
kept stopping, and every stop was correct behaviour - an unread counter is
refused, never rounded to the nearest exemplar, because a wrong counter
silently corrupts the model. That happened once and is worth remembering: with
only 0 and 1 present, an SS "2" matched the "1" exemplar above the 0.80 gate,
the solver recorded green=1 for a row that truly read 2, and every candidate it
computed afterwards came from a false premise.

Note the last stall of the winning run was the digit **6** - the win condition
itself. The game acted on it and cleared the stage while the solver could not
read it, so the mission succeeded without the bot knowing why.

## TP NOW BANKS — and the last blocker was a hand-picked threshold

Five TP missions banked in one session across all three families: hand-seal
(including eight-sign rounds), memory cards (20/20 and 10/10), and the kekkai.
Before this, every pass read "N started, 0 banked".

**THE CAUSE WAS FIVE THOUSANDTHS.** `close_out` confirmed the village with
`_tpl("lobby_rail_fortune", 0.90)` - a threshold invented at the call site. The
anchor's CALIBRATED threshold is 0.88; 0.90 was reasonable when it measured
~1.000, which it does on wgpu. On webgl the same anchor reads **0.895**, so the
village could never be confirmed and a won mission was reported as a failure.
One second apart in the log:

    15:56:40  Success panel cleared but the village did not come back; not
              calling this a success
    15:56:41  resume: lobby (lobby_rail_fortune conf=0.895)

The resume ladder, on the calibrated 0.88, found it instantly. A threshold is
calibrated ONCE against measured extremes; every call site that re-guesses it
is a place where one backend, one animation frame or one re-cut silently
changes an outcome. `lobby_rail_fortune` also has a webgl variant now, so it
reads 1.000 there and the margin is real rather than marginal.

### `close_share_x` CLOSES THE SUCCESS PANEL ITSELF

There is often no separate share prompt to dismiss. Measured live:
`close_share_x` matched 0.998, was clicked, and the next frame was the VILLAGE
with the gold and level both up - the template had matched the reward panel's
OWN close button, and the mission was already banked.

The old code went straight on to hunt for the green check on that village
frame, failed, and logged *"Success panel up but its check was not located
(0.609)"* about a panel that no longer existed. **That one misleading line sent
this investigation to measure a check glyph on a frame with no panel in it,
twice** - a scale sweep peaked at 0.699 on village scenery. So the panel is now
RE-VERIFIED after any dismissal, and if it has gone the village confirmation
decides, which is the measurement that actually establishes a banked mission.

**AND SAVE THE FRAME YOU SCORED, NEVER A FRESH CAPTURE.** The "save the frame
that defeated us" instrumentation re-captured at save time, so it wrote the
village - evidence that looked exactly like a panel whose check could not be
found. Worse than no evidence, because it was confidently wrong. It saves `f`,
the frame the decision was made on.

## THE KEKKAI DIGIT READER — key on the INK, not on a white outline

The kekkai stopped after guess 1 every time: "could not read row 0 (green
0.410 / gold 0.897)". The geometry was never wrong - overlaid on a saved frame,
the coordinates land exactly on the two feedback discs - and neither was the
solver. `digit_mask` thresholded BRIGHT pixels, because on wgpu the glyph is a
dark digit with a WHITE OUTLINE. webgl draws no outline, so:

    green disc (dark)   bright fraction 0.08  - the "0" VANISHED, leaving only
                                                a specular highlight
    gold disc (light)   bright fraction 0.375 - the DISC went white and the "1"
                                                became a dark HOLE in it

The ink is dark in BOTH renderings - measured p1=17 against a green disc body
of 112, and p1=28 against a gold body of 198 - so that is what to key on.
Taking the darkest 30% inside a disc-shaped window (which also excludes the
parchment and the disc's dark rim) gives a clean legible glyph on either disc,
at consistent mask fractions of 0.150 and 0.153 where the old mask gave
0.08 and 0.375.

**THE OLD EXEMPLARS ARE NOT REUSABLE, AND THAT WAS MEASURED BEFORE RELYING ON
IT.** Scored against ink masks of a known 0 and 1, all sixteen outline
exemplars sat at 0.33..0.63 distance and the ink "1" matched `2.png` BEST - a
wrong answer. An outline and a silhouette of the same glyph are different
shapes, which is also why this file's earlier attempts to normalise between the
two measured worse. So the ink set lives in `ref/auto/tp/digits_ink/` and
`digits/` is left in place, unloaded, as the record of what wgpu draws.

**IT IS DELIBERATELY INCOMPLETE.** Only two digits could be labelled with
certainty from a saved panel - a "0" on the green disc and a "1" on the gold -
so the gate has to REFUSE the rest rather than round them to the nearest
exemplar. Verified: with only the wrong exemplar available, a 0 scores 0.099
and a 1 scores 0.206 against the 0.80 gate, so both are refused; with the right
one, both read 1.000. A wrong counter corrupts the solver's model silently,
which is far worse than an unread one.

Two exemplars turned out to be enough for a live solve, because early feedback
counters are small:

    guess 1  Green,Red,Blue     pool 216   feedback green=1 gold=1
    guess 2  Green,Green,Red    pool  24   feedback green=1 gold=0
    guess 3  Green,Blue,Black   pool   8   panel closed -> SOLVED

A 2 or a 3 will still be refused and saved for labelling, which is the correct
failure and how the set grows.

## THE COLD START HAD NO RENDERER TO READ

`main` asks for the backend once, at startup - but a cold SWF takes 25-30 s, so
on a fresh launch there is no `ruffle-player` yet:

    renderer: no canvas context yet (no ruffle-player)
    renderer unknown, so the default templates stand

and nothing re-asked. A session that started before the game finished loading
therefore ran on the WRONG template set for its entire life, which on webgl
means the farm cannot leave the village - the exact failure the variants exist
to fix. It only came right in practice because the operator happened to switch
the renderer by hand, which reloads and re-reads.

`Runner.ensure_renderer_templates` asks once per cycle and stops the moment it
gets an answer, so the steady-state cost is nothing. It does not re-ask once
known: the renderer cannot change while the document stays put, and
`_apply_game_setting` already covers the case where it does.

## THE HAND-SEAL GAME WAS NEVER FAILING TO MEMORISE — it clicked too early

Five TP missions, zero banked, and the board explained it in one glance:
`Skill : 1 / 4` with **all three hearts** after the bot had "played" a round.
Nothing had been submitted, right or wrong.

**THE CLICKS WENT OUT DURING THE LOOK PHASE.** The sequence is read while the
slots display it, and in that phase the tiles are face up but GREYED - a click
on a greyed tile does nothing. The reader went from "recorded 2 of 2 sign(s)"
straight to clicking about two seconds later:

    15:05:12  look phase: recorded 2 of 2 sign(s), in order
    15:05:14  CLICK hand sign 0 = tile 1
    15:05:15  CLICK hand sign 1 = tile 8
              board still `Skill : 1 / 4`, three hearts

`wait_input_phase` fixes it, and the size of the wait is the proof: measured
live it blocks for **9 to 19 seconds** depending on the sequence length. The bot
had been clicking two seconds in, every time.

**IT CANNOT GATE ON `tiles_live` ALONE, AND THAT IS THE INTERESTING PART.**
The blue-glove check is the right primary signal on wgpu, where greying drives
the glove's blue fraction to exactly 0.000. On webgl it returns instantly and
fixes nothing, because **webgl does not apply the grey-out filter at all** -
the glove keeps its blue and the tiles read LIVE throughout the look phase.
Independently measured on the skill slots, where webgl greying left saturation
at 0.397..0.748 against 0.000 on wgpu. Greying is a COLOUR operation, and
colour is what this backend renders differently.

So the second signal is the SLOTS, which flip back to NINJA SAGA card backs
when input opens - a different IMAGE, not a recolour, and therefore
renderer-independent. Both must hold: on wgpu they agree, on webgl the
always-true tiles check reduces the condition to the slot signal.

### `d = 0.000` IS AMBIGUOUS, AND BOTH READINGS ARE WRONG ALONE

The old check compared the filled slot against the memorised sign and called
`d=0.000` a match. This file had already warned that zero means "nothing
happened". **Both of those are half right, and that is why the HUD had to be
read instead.** Two opposite outcomes produce exactly 0.000:

    nothing happened   the slot is STILL showing the prompt we memorised, so
                       we compared a crop with itself
    a CORRECT pick     the slot now shows that seal drawn as slot art - the
                       very rendering we memorised - identical for the right
                       reason

Measured both, in that order. So it is UNKNOWN and never decides a round.

### THE BOARD'S OWN HUD IS THE VERDICT — hearts and the round counter

`seals.hud_state` / `hud_verdict` read two anchor-relative boxes beside the
"Skill :" HUD and answer the question the slot art cannot:

    hearts dropped     a WRONG answer was submitted -> the clicks DID land
    counter changed    the round advanced -> they landed and were right
    neither moved      NOTHING was submitted -> they did not land

**No digit recognition is needed**, which is the point: hearts are counted as
red blobs (measured 2738..2742 area, 67x56, three of them) and the counter is
compared as an IMAGE against the pre-answer snapshot. That sidesteps the digit
exemplar problem entirely - see the kekkai section below for what happens when
you do depend on reading digits. Verified against synthetic heart-loss and
counter-change frames, plus a lobby frame for the unknown case.

Only "advanced" counts as a played round now. Our own slot check is not
allowed to decide it.

## WHY IT SOMETIMES LOST HEARTS — the catalogue was never built

With the clicks landing, the remaining failures correlated perfectly with one
number. Measured over one mission, with the HUD as the verdict:

| round | thinnest margin | board |
|---|---|---|
| 1 | 9.32x | advanced |
| 2 | **1.01x** | HEART LOST |
| 3 | 5.78x | advanced |
| 4 | 3.85x | advanced |
| 5 | **1.06x** | HEART LOST |

Both failures were ~1.0x; everything at 2.6x or better was accepted. The bot
memorises the sequence perfectly - what fails is mapping a seal drawn SMALL ON
A SLOT CARD over animated flames to the same seal drawn LARGE IN A WOODEN
FRAME. Two of the ten have near-identical blue-glove silhouettes, and on both
failures the ink tiebreak was consulted and **agreed with the wrong answer**.

`load_catalogue()` exists precisely to remove that guess - and it reads
`ref/auto/tp/seal_catalogue/`, **a directory that had never been created.
There was no builder.** So the catalogue was always empty, `identify()` always
returned None, and every round fell through to the weak cross-rendering path.

`engine/build_seal_catalogue.py` builds it from the 288 crops already on disk,
by the method `load_catalogue`'s own docstring specifies:

* split by CROP SIZE, not filename - 104x104 is tile art, 140x170 is slot art.
  Sizes are a property of the thing; the filenames were written by several
  different code paths and are not uniform.
* cluster WITHIN each rendering, which is the strong direction. Both distance
  distributions are bimodal with an EMPTY GAP, so the threshold is a
  measurement: tile pairs 0.000..0.104 then nothing until 0.156. Tile gives 10
  clusters for every threshold from 0.05 to 0.15 and slot gives 10 from 0.03 to
  0.12, so neither balances on a knife edge.
* link the two renderings ONCE by a one-to-one assignment on mean cross
  distance. **One-to-one is the whole point**: nine seals link at 2.72x..12.97x,
  and the tenth links at **0.95x - its runner-up is CLOSER than its
  assignment** - so it is settled BY ELIMINATION, which is exactly the leverage
  the live matcher lacks because it decides each sign independently.

Validated: **429 harvested crops, 100% correct, 0 wrong, 0 unknown** from four
medoid exemplars per seal per rendering. Then live:

    round 1  [8, 0]                    advanced, 3 hearts
    round 2  [9, 2]                    advanced, 3 hearts
    round 3  [4, 5, 2, 8]              advanced, 3 hearts
    round 4  [7, 2, 1, 4, 9, 3]        advanced, 3 hearts
    round 5  [2, 5, 3, 6, 8, 7, 1, 9]  board closed - 5/5 done

**Zero hearts lost**, where the same mission had been losing two. Catalogue
margins run 5.72x..70.42x against the old 1.01x. Note round 5 was EIGHT signs:
the sequence length was never the problem - "recorded 8 of 8 sign(s), in order"
was already working.

**A UNION-FIND BUG NEARLY HID ALL OF THIS.** The first clustering used path
halving written as `lab[a] = lab[lab[a]]; a = lab[a]`, which does not settle on
a single root, and the tell was that the cluster sizes SUMMED TO MORE THAN THE
INPUT - 220 members from 208 crops. A partition whose parts do not sum to the
whole is not a partition, and its cluster COUNT cannot be trusted either. It
reported 9 slot clusters where there are 10.

## A GUARD DEFEATED BY A LABEL — the fourth instance of one bug

The bot span on one screen for about ten minutes: `resume: no anchor matched`
reaching **streak 634** with no relog and no pause.

`Runner.step` drives `Resumer.advance()`, which has no `max_unknown` of its own
(only `Resumer.run()` does), so the runner bounds the streak itself - a guard
added after 52 consecutive unrecognised cycles. It was then defeated by a
diagnostic label:

    self.state = info.get("step", out)        # "unknown" - the ladder's verdict
    if self.state == "unknown":
        self.state = self._name_screen() or "unknown"   # -> "seal_entry"
    ...
    if self.state == "unknown": self.unknown += 1 else: self.unknown = 0

`_name_screen` deliberately recognises the TP minigame HUDs, so on a hand-seal
board - a screen the ladder cannot climb from - the label came back non-unknown
and **the counter reset to zero on every cycle**.

This file's own rule names it: *a guard bounded by a counter is only as good as
the event that CLEARS the counter; clear it on evidence of progress, never on
something merely correlated.* Now the ladder's own verdict is captured BEFORE
anything relabels it, so naming a screen for the operator can no longer be
mistaken for making progress on it.

Previous instances, for the pattern: reset on reaching the lobby (a relog
always does), reset on a task returning (a swallowed crash also does), reset on
a lap that banked nothing. This is the fourth.

## THE KEKKAI DIGIT READER DEPENDS ON THE MISSING TEXT STROKE

The kekkai stops after guess 1 - "the first 3 choices", i.e. the three rune
clicks - with:

    could not read row 0 (green 0.410 / gold 0.897)
      at green=(1997, 292) gold=(2083, 292)

**The geometry is correct.** Overlaid on the saved frame, those coordinates land
exactly on the green "0" and gold "1" feedback discs. The solver is fine too.
What fails is reading the digits, and the reason is the same as everything else
on this backend.

`digit_mask` binarises BRIGHT pixels, because on wgpu the glyph is a dark digit
with a WHITE OUTLINE and thresholding bright pixels captures that outline.
**webgl draws no outline**, so:

    green disc (dark)    bright fraction 0.08 - the "0" vanishes entirely and
                         only the disc's specular highlight survives
    gold disc (light)    bright fraction 0.375 - the DISC is white and the "1"
                         is a dark HOLE in it

Measured inside the discs, the ink is cleanly separable - green p1=17 against a
disc body of 112, gold p1=28 against 198 - so a DARK-INK mask would work on
both backends, since the ink is dark in both. The obstacle is not the idea:

**every existing exemplar is stored as a white-outline MASK** (bright fractions
0.24 green / 0.46 gold), so changing the mask function invalidates the whole
set, and the raw crops they came from were never kept. Re-deriving them needs
fresh captures of each digit on each disc. Not attempted here rather than
half-done, and note this file's existing warning that normalising the two forms
was tried and measured WORSE.

Until then the reader refuses to guess and saves evidence - and it now saves
**the whole frame plus the coordinates it used**, not just the two 68x68 crops.
That mattered: the crops alone showed one near-solid black square and one white
SPIRAL, which is a rune glyph, and nothing in a binarised 68x68 crop says
whether the reader was misreading a digit or looking in the wrong place. The
full frame answered it in one look.

## STILL OPEN after all of the above

* **the TP Success panel's check reads 0.609** against `green_check`'s 0.80
  gate, so a played mission is not acknowledged and nothing banks. The same
  glyph dismisses the FARM's Mission Success at 0.970 on the same backend, so
  the template is not broken in general. The panel is transient and escaped
  capture twice, so `tp.py` now SAVES IT when the check is not located.
* **the kekkai digit exemplars**, above.

### AN ENEMY ON A SLIVER IS INVISIBLE TO THE BAR FINDER

Measured on a live Eudemon SS boss (`ref/auto/battle/party_boss_sliver.png`):
`Izo` was plainly on screen at roughly 4% HP and `find_enemy_bars` returned
NOTHING. Its bright-red run is **13 px** against the function's
`min_run = 40`, so any enemy below about 13% HP drops out of the scan
entirely.

    battle: no enemy HP bar located this turn      x4 in a row, near the kill

It fails SAFELY - the caller already refuses to feed the watchdog a fake
reading, so a missing measurement can never trigger an abort - but the
consequence is that `DamageWatchdog` goes blind exactly when a fight is nearly
won, and a fight where every enemy is on a sliver shows no progress at all
while being one hit from over. It also means flat HP readings near a kill are
NOT evidence of a stall; they are the bar shrinking below the detection floor.

**Do not simply lower `min_run`.** 40 is what keeps noise out of a scan that
this file already records returning the player HUD as enemy bars. A fix wants
the bar's TRACK (the dark empty channel is full length at any HP) to locate
the bar, and the red run only to measure its fill - a different shape of
detector, not a threshold nudge.

Also worth recording from that frame, because it retires a worry rather than
adding one: **teammates do not contaminate the scan.** The runner calls
`find_enemy_bars` with `x0 = 55%` of the frame width, and the party stands on
the LEFT with the player while enemies are on the right - three party-full
frames each gave exactly one bar. The `enemies=4` reading seen once in the
same fight was a transient, not the party.

## AN ACCIDENTAL SCROLL MUST NOT MOVE THE GAME — lock it in the PAGE

The operator: "sometimes I accidentally scroll down which causes some kind of
behaviour bug". It does, and this file already explains why - every minigame's
geometry is ABSOLUTE, so a displaced game breaks all of them at once and each
one reports a fault in its own subsystem. Measured previously: scrollY 60 put
the game at **-236 captured px** and the memory board's rows measured -237 out.

`__nsbotAlign` already put the scroll back, but it is only called from
`ensure_focus`, which runs BETWEEN CYCLES - and a mission blocks for minutes.
So an accidental scroll during a mission stood for the whole of it.

**The fix belongs in the page**, for the same reason the panel's heartbeat does:
Python is not there to notice. `__nsbotScrollLock` layers three mechanisms,
because each catches what the others miss:

    1. html,body{overflow:hidden !important}   removes the scroll rather than
                                               reacting to it, and an
                                               !important stylesheet beats the
                                               site's inline styles
    2. a `scroll` listener that snaps to 0     catches whatever still scrolls -
                                               keyboard, the site's own script,
                                               a focus() on some element.
                                               `scroll` cannot be prevented,
                                               only undone
    3. wheel/touchmove preventDefault          stops the gesture before it
                                               scrolls, so there is no visible
                                               jump-and-snap

Verified live, all four behaviours at once:

    scrollTo(0, 400)                  -> snapped back, scrollY 0, snaps 1
    a real wheel via Input.dispatch   -> scrollY stayed 0, nothing to undo
    a five-event flick                -> scrollY stayed 0
    the dock's own log pane           -> still scrolls (0 -> 40)

**THE PANEL'S LOG PANE MUST STAY SCROLLABLE**, so layer 3 exempts any event
inside the dock; layer 1 cannot affect it, being `position:fixed` with its own
scroller. **KEYS ARE DELIBERATELY NOT BLOCKED** - this is a Flash game and the
SWF may want them; a swallowed keystroke would be a new bug to chase, and
layer 2 covers a keyboard scroll after the fact.

**IT RIDES WITH FOCUS MODE, and that is a correctness decision rather than a
convenience.** With focus ON the game is pinned at scrollY 0 and any scroll is
an accident. With focus OFF the bot SCROLLS ON PURPOSE - `Capture.scroll_game`
is the documented fallback for reaching an anchor in the hidden 119 px band,
and the resume ladder alternates the scroll before calling a frame
unrecognised. Locking there would break the one path that needs it.
(`scroll_game` already no-ops under focus mode, so nothing else changed.)

`align()` re-asserts the lock every cycle because a reload drops the injected
style and its listeners - the standing rule that any cached belief about page
state is invalidated by a navigation. Snaps are COUNTED and the count is read
from the page, so an accidental wheel becomes a log line -
`scroll: 1 accidental scroll(s) snapped back (the game did not move)` - instead
of a mystery drift. It is pure DOM, so it behaves identically on every backend.

## TP UNDER webgl — one variant, and the rest was already fine

Tested end to end on webgl. Only `special_tab` needed recutting (0.795 against
its 0.88 gate); everything else on the TP path held:

    Special tab       1.000  (the new variant)
    TP Training row   0.998
    TP list           3 rows found at y=[462, 638, 818]
    minigame classify seal_entry {'seal_hud': 0.998}, cards {'cards_hud': 0.986}
    hand-seal round 1 sign 0 -> tile 2 (margin 6.63x), sign 1 -> tile 1 (10.58x)
    memory cards      20/20 cells cleared BY THE GAME, 40.1 s

So the blue-glove sign matcher and the mean-HSV card signature both survive the
backend change - the two hardest pieces of perception in the project, and
neither needed touching. Note `tp_training_row` measured 0.282 on the Mission
Room screen, which is a CORRECT NEGATIVE (it lives on the TP list, not there) -
scoring a template on a screen that does not contain it proves nothing, and
doing so once already produced five bogus "broken" verdicts in one pass.

Two things stop TP, and NEITHER is a rendering fault:

* **the hand-seal round 2 PARKS** - "no Start, and the tiles are already live".
  That is the open issue this file already records ("the slot row is both the
  prompt AND the input"): by the time the bot looks, the round is in its INPUT
  phase and the sequence is gone. The bot correctly refuses to click at random.
  Worth noting the timing - round 1 finished at 14:45:17 and the bot looked at
  14:45:20, which is `play()`'s 2.5 s inter-round sleep. Whether the reveal
  happens inside that window is UNMEASURED; the experiment is to burst-capture
  straight through a round transition rather than sleeping and then looking.
* **the TP Success panel's check read 0.609** against `green_check`'s 0.80 gate,
  so the reward was not acknowledged ("Success panel cleared but the village
  did not come back; not calling this a success"). The same `mission_start`
  glyph dismissed the FARM's Mission Success at 0.970 on the same backend
  minutes earlier, so the template is not broken in general - this particular
  panel is either drawing the check outside the 0.95..1.95 sweep or drawing it
  differently. Unmeasured, because the panel was gone before it could be
  captured and reaching it again costs a TP mission from the day's list.

### A STALE LOG NEARLY PRODUCED A WRONG DIAGNOSIS, again

While reading the above, `farm` lines appeared to be interleaved with a running
TP task, which would have been a serious bug - two tasks acting at once. They
were not. `run/app.log` is APPENDED ACROSS SESSIONS, and the farm lines sat at
line ~12095 while the session being read started at line 15593: same wall-clock
times, different days.

This file already warns about exactly this ("check `ls -l run/app.log` against
the clock before trusting it"). The warning is not enough on its own, because
the trap is not a stale FILE - it is stale lines inside a live file. **Anchor
the read to a line number**, e.g. the session's own startup line, and filter
from there. Timestamps alone cannot separate sessions.

## THE BACKEND CHANGES THE PIXELS — one template set is not enough

The operator switched to `webgl` and the farm could not leave the village.
Measured live, same session, same screen, minutes apart, only `renderMode`
changed:

| anchor | wgpu-webgl | webgl | canvas |
|---|---|---|---|
| `mission_room_entry` | **0.997** | 0.531 | - |
| `char_slot_level` | **0.965** | 0.677 | 0.678 |
| `character_select` | 0.865 | 0.442 | 0.436 |
| `result_panel` | **1.000** | 0.341 | - |
| `mission_success` | **1.000** | 0.418 | - |
| `lobby_logo`, `nav_*` | 0.93..1.00 | 0.95..1.00 | - |

**THE SPLIT IS NOT "LEGACY WEBGL IS BROKEN".** `webgl` and `canvas` agree with
each other to **0.001** and both disagree with the wgpu backend. wgpu draws
text WITH its stroke; the other two draw a thinner, unstroked face at a
different baseline, and shift some element colours (a gold button beside the
Mission Room plaque renders purple). Everything that is not text is
**pixel-identical** - mean |diff| 3..7 across the plaque, with the whole
difference confined to the text rows.

So every template in this project is calibrated against the OUTLIER, and one
variant set serves both of the others.

    icons survive     nav_*, lobby_logo, the command discs, the green check
    text does not     any crop whose discriminating content is lettering

**A LOWER THRESHOLD CANNOT FIX IT.** `mission_room_entry` at 0.531 sits INSIDE
the negative distribution - other anchors read 0.39..0.53 on the same frame -
so a lower gate buys false positives, not recognition.

### `tpl/<renderer>/<name>.png` overrides the default crop

`perceive.set_renderer(name)` is module state, set once from
`browser.renderer_info()["requested"]` - **`loadedConfig`, never the canvas
context type**, which cannot tell `wgpu-webgl` from `webgl` because both draw
through WebGL2. `load_templates` and `perceive.template` then prefer
`tpl/<renderer>/`, and the log says which were substituted, because a silently
swapped template is indistinguishable from a mis-cut one when a match later
goes wrong.

Six variants take the farm from "cannot leave the village" to a mission banked
end to end on webgl (3 battles, 2 victories, closed out to the lobby):
`character_select`, `char_slot_level`, `play_btn`, `mission_room_entry`,
`result_panel`, `mission_success`. Everything else - `grade_tab`, `page_next`,
`mission_row`, `mission_start`, `cutscene_continue`, the command bar,
`action_flag`, the HP bars - held with no variant at all.

**THE MECHANISM WAS INERT AT FIRST, AND THE LOG SAID IT WAS WORKING.**
`load_templates` was swapping crops correctly and printing "4 recut for
webgl", while the running bot logged `could not reach the grade panel` on every
lap - because **nine live sites built `Template(name, "tpl/<name>.png")`
directly** and never went near `load_templates`:

    farm._tpl   minigame._tpl   tp._tpl   cards._HUD
    kekkai_play (x3)   seals (x2)

Measured at the time: the variant scored **1.000 on ten fresh lobby frames**
while the bot could not see it. `perceive.template` is now the only way to
build a template by name, and the suite greps all nine modules for a bare
`tpl/` path. This is "one coordinate space, or none" in a new costume - a
mechanism that covers only some of its call sites is the half-applied
correction this file already has a rule about.

### Recutting: `engine/recut.py`, and why the naive cut is useless

**Cut the same region out of a webgl frame and you get a template that matches
its own frame at 1.000 and false-positives everywhere.** `mission_room_entry`:
self 1.000, worst negative **0.907** against an 0.88 gate. Strip the stroked
text out of that plaque and what remains is smooth pale pink, and a
low-variance template correlates with any large smooth region - the same trap
as `close_popup_x_large` ("flat 0.547 at every scale. Bad crop") and the
flood-filled digit masks.

The cure is to give the crop back STRUCTURE by expanding into surrounding art,
and the expansion is measured, never guessed:

    pad     0   self 1.000   worst-neg 0.907    unusable
    pad    20   self 1.000   worst-neg 0.780    unusable
    pad    40   self 1.000   worst-neg 0.665    chosen
    pad   140   self 1.000   worst-neg 0.475

**EXPAND SYMMETRICALLY.** The match centre is what gets CLICKED, so an
asymmetric crop silently moves every click that anchor drives. The tool
asserts the centre does not move, and the suite pins the structural invariant:
a variant is the default's size plus EQUAL, EVEN padding on both axes.

Acceptance is calibrated to the company these anchors keep - every margin in
this file sits between 0.37 and 0.66 - so negatives must stay at or below
`threshold - 0.18` with at least 0.30 of separation. **A first version scored
`self - max(neg, threshold)`, which is a CONSTANT whenever negatives are below
the gate; every expansion reported "+0.120" and nothing could ever qualify
while pad 0 was in fact fine.**

### A DETECTOR-ONLY ANCHOR NEED NOT BE SYMMETRIC — but constrain the search

`result_panel` defeated symmetric expansion outright: every pad from 0 to 140
left its best negative between 0.70 and 0.85, because the banner is a large
flat parchment strip and expanding only adds more parchment.

Symmetry is only needed because a centre gets clicked, and `result_panel`'s
does not - `resume.py` uses it as a rung ANCHOR whose clicked target is
`mission_start`, and this file already records that the panel body is not a hit
area (a Victory absorbed **eleven** clicks at the canvas centre). So
`recut.py free` searches sub-windows, behind a required `--no-click-anchor`
acknowledgement.

**ITS FIRST ANSWER WAS A TRAP, AND THE SCORE LOOKED GREAT.** Searching a band
around the centre, it picked a window that was **not the panel at all**: the
team badge reading `0/2` plus sky and cloud art from above the banner, at a
+0.576 separation. Two faults in one crop:

* it contains a **counter**, and an anchor must not contain the thing that
  varies - the same mistake `tp_seal_hud` was re-cut for;
* it would match in ANY battle, so it does not detect the state it is named
  after.

It scored well because **the negatives are all wgpu frames** and that HUD
renders differently there - a separation earned against the wrong question.
Candidates are now constrained to sub-rectangles OF THE DEFAULT TEMPLATE, so
every crop is structurally a piece of the anchor - the property a score cannot
check for itself. The accepted crop is the letters `tory` from "Victory!",
self 1.000, worst negative 0.576, separation +0.424.

**General limit, and it bounds everything above: calibrating a variant for
backend X really wants NEGATIVES rendered by backend X.** The committed `ref/`
frames are wgpu. For a crop dominated by art that is fine, since the art is
identical between backends; for anything else the false-positive bound is
weaker than it looks. `ref/auto/renderer/` holds the webgl fixtures harvested
so far and is excluded from the negative set, because scoring a positive as a
negative rejects every crop.

### The cross-check that must never be skipped

A Victory must not bank a success. Measured on webgl frames of both panels,
both directions:

| webgl templates | webgl Victory | webgl Mission Success |
|---|---|---|
| `result_panel` | **1.000** | 0.524 |
| `mission_success` | 0.244 | **1.000** |

### Mission Success unrecognised = the fifth "negative definition" bug

Before its variant existed, a completed mission left "Mission Success!" on
screen at 0.418, so `looks_like_mission_scene` found nothing to veto with and
the runner TRAVERSED on top of the reward panel - 13 dead ends, while
`find_character` locked onto the panel's red stamina icon at a byte-identical
`(1567, 699)` every pass. The mission had actually succeeded (3900 gold,
99892 XP on screen).

That is this file's recurring shape for the fifth time - mission list,
battle-between-turns, seal-broken dialog, Level Up, and now a result panel
under a different renderer. **A byte-identical detector coordinate across
passes is always a static object, never a character.**

### THE WHOLE RESULT-PANEL CHAIN VERIFIED ON webgl

Reported from Windows: *"the victory screen was stuck there as it couldn't
see the check button."* Not reproducible on this machine with current code. A
full Grade-A mission on webgl, end to end:

    battle 1 -> victory   result_panel conf=1.000
                green check found at scale 1.2  -> dismissed (0.970)
    battle 2 -> victory   result_panel conf=1.000  -> dismissed (0.970)
    battle 3 -> victory   ended in a cutscene
                mission_success recognised, check at scale 1.8 -> (0.970)
    SUCCESS after 3 battles, closed out to the lobby, banked

Both check sizes the panels use — 1.2 for a mid-mission Victory and 1.8 for
Mission Success — are found at 0.970 on webgl with the DEFAULT crop. That is
consistent with what this file already measured: only TEXT loses its stroke
between backends, and the check is art, which is pixel-identical.

**So the Windows symptom is most likely the missing commit, not the check.**
That machine was tested while `75053db` was unpushed, and without
`tpl/webgl/` the `result_panel` variant does not exist — measured 0.341 on a
webgl Victory. The panel is then not recognised AT ALL, and this file already
records what that produces: the runner treats the reward panel as scenery and
"traverses" on top of it. From outside that is indistinguishable from being
stuck on the victory screen unable to press the check.

### AND TWO FIXTURES THAT WERE LYING ABOUT THEMSELVES

Diagnosing the above, two saved frames produced a confident wrong answer:

    success_panel_webgl.png            is the SPECIAL TAB
    success_check_missing_...png       is the VILLAGE

The second is an artefact of the re-capture bug this file already documents
("save the frame you SCORED, never a fresh capture") — it was written before
that fix. Scoring the check glyph on them gave 0.543 and 0.633, which looked
exactly like the reported failure and is in fact a CORRECT NEGATIVE on screens
containing no check.

This is the same lesson as `tp_training_row` measuring 0.282 on the Mission
Room: **scoring a template on a screen that does not contain it proves
nothing.** Check what a fixture actually shows before quoting a number off it.
Both are renamed for what they are.

A second self-inflicted error in the same pass: `result_panel [webgl]`
appeared to false-positive on 89 frames, which would have been serious. It was
an invented 21-scale 0.95..1.95 sweep applied to a 47x183 crop. At its
CONFIGURED scales it matches exactly one frame in the whole reference set, the
real webgl Victory, at 1.000. **A template's scale list is part of the
template**; overriding it and then judging the result measures the override.

### A SATURATION GATE IS PER-RENDERER TOO — and webgl has no calibration

Template variants fix template anchors. They do nothing for the detectors that
read COLOUR, and `combat.slot_cooling` is one: it asks what fraction of a skill
tile's interior clears a saturation threshold. Measured on the committed
refusal frames:

    wgpu-webgl    cooling 0.000  0.000  0.000        ready 0.905
    webgl         0.397 .. 0.748 (used slots)        0.865 .. 0.904

wgpu is cleanly bimodal, which is what the 0.25 gate was calibrated against.
On webgl, greying does NOT drive the tile to zero saturation - the readings run
continuously, exactly as this file already records for the FIRST saturation
attempt ("56..191 continuously, with a pale slot reading 56 while fully
ready").

**With the wgpu gate applied, all four webgl tiles read READY - including the
slot that had just visibly refused.** So the bot clicks a cooling skill and
pays a full ~6 s resolve timeout for it, twice a turn. Observed live:
`S1 timed out`, `S3 timed out`, `2 actions did not resolve` inside one turn,
with turn gaps of 6.7..10.4 s.

So `COOLING_GATES` is keyed by backend, `wgpu-webgl` keeps the measured 0.25,
and **an uncalibrated backend returns None (UNKNOWN)** rather than a confident
wrong answer. The caller already offers an UNKNOWN slot anyway, so behaviour is
unchanged - the difference is that the log stops claiming a greyed slot is
ready. A renderer of `None` keeps the historical path exactly, so every offline
tool and test is untouched.

**WHY IT IS NOT SIMPLY RE-CALIBRATED.** It looks like a gate near 0.80 would
separate the webgl readings, and that is a trap. All EIGHT webgl refusal frames
harvested over 21 minutes read S1=0.748 S2=0.904 S3=0.397 S4=0.865 - identical
to three decimals - because the rotation is always S1 S3 S4 S5, so the same
slots are cooling at the same point of every turn. That is ONE battle state
sampled eight times, with ~0.05 of margin, and S4's true status is inferred
rather than known: the filename only names the slot that refused. Calibrating
from it would repeat the mistake this file records about the OpenCV thread
sweep - "before trusting a measurement, check that the knob you turned is
connected to anything", and its cousin, that a suspiciously constant reading is
usually not data.

To calibrate properly, harvest frames where every slot's state is KNOWN - drive
a rotation that leaves some slots deliberately unused, and record the expected
state alongside each frame.

**A TEST THAT PASSED FOR THE WRONG REASON, exposed by this.** The greyed-skill
test globbed `ref/auto/battle/cooldown_msg_*.png` and applied ONE battle's slot
expectation to every frame it matched. `_keep_failed_frame` WRITES NEW FRAMES
DURING LIVE PLAY, so the moment webgl runs harvested six more, the test began
asserting one battle's states about a different battle on a different backend,
and failed. A fixture set that grows at runtime cannot carry a hardcoded
expectation - the two calibrated frames are now named explicitly.

### `webgpu` never came up

Asking for `webgpu` produced no canvas at all on this machine ("shadow root",
no context), so it is untested rather than broken. `canvas` works and renders
text like `webgl`, at a 2d context.

### Performance was never the reason to switch

The operator wanted `webgl` because it is the lightest backend. Measured rAF
in the game's own frame, one clean pass per document:

    webgl        119.7 fps
    wgpu-webgl   119.9 fps

Both pinned to the display refresh, matching `docs/BENCHMARK.md` (120 fps,
188x GL headroom, against a 24 fps SWF). There is no render-intensity
difference to buy here - the bot now works on both, so the choice is free.

**`renderer_ab.measure_fps` IS NOT SAFE TO CALL TWICE on one document.** It
installs a fresh rAF chain per call without cancelling the previous one and
they all increment the same counter: three calls read 119.6 / 239.3 / 359.8.
The A/B harness only escapes it because each backend is preceded by a reload.

## THE LADDER WOULD HAVE SPENT TOKENS — a choice is not an acknowledgement

The most serious defect found so far, and it was live.

Losing a Eudemon boss raises **"Do you want to revive by using 50 token?
(Revert 30% HP)"** with a GREEN CHECK and a RED X. The `confirm_dialog` rung
acknowledges a lone green check generically - and it matched that check at
**0.979**, with the check itself as its click target. The next ladder pass
would have spent 50 of the premium currency this file's safety rules say must
never be spent. Nothing was spent only because a relog happened to clear the
dialog first.

**The distinction the ladder was missing is structural, and needs no new
template:**

    one green check              an ACKNOWLEDGEMENT  -> pressing it is safe
    a green check AND a red X    a CHOICE            -> pressing green ACCEPTS

Measured on the live prompt (`ref/auto/battle/revive_prompt.png`):

    green check  centre (1622, 847)  80x81
    red X        centre (1897, 850)  82x83

Same size, same row, 275 px apart. Everything the ladder already handles - the
seal-broken dialog, Level Up, a Victory panel, a mission detail panel -
carries a check ALONE.

`perceive.choice_dialog` finds the pair and `Resumer.advance` presses the RED
one. Three decisions worth keeping:

* **The veto is consulted ONLY where a green check has already matched.** As a
  free-standing detector it fired on 4 of 125 reference frames; scoped to the
  one rung that clicks a check, that exposure disappears. A safety check that
  fires on unrelated screens would licence clicking red things at random.
* **The order is never assumed.** The function does not require green to be on
  the left, because a variant with them swapped would otherwise go undetected
  and fall straight through to the rung that clicks green.
* **Declining is the safe direction.** A wrongly declined dialog costs one
  retry; a wrongly accepted one costs tokens, and this project cannot buy them
  back.

**The general rule, and it generalises past this one prompt:** before pressing
a control because it is the affirmative one, check whether the screen is
OFFERING A CHOICE. The ladder's whole design is "the only control on this
screen is the check, so pressing it is safe" - and that premise silently
stopped being true the first time the game asked a question.

### AND THEN IT FIRED DURING AN ORDINARY BATTLE — the scoping was the fix

The decline above shipped, and the guard clicked the TURN-ORDER MARKER in the
middle of a farm fight, twice in one battle:

    gate: a dialog is blocking this wait and offers a CHOICE
          (green (2112, 949) / red (2346, 962)) - declining
    CLICK px=(2346,962) decline a blocking two-button dialog

Neither is a dialog button. The "green" is a skill-slot icon, the "red" is the
turn marker - two coloured discs on one row, which is all the shape test asked
for.

**The regression was moving the guard without carrying its scoping.** In the
ladder it was consulted ONLY where a green check had already matched, and the
note above says exactly why - free-standing it fired on 4 of 125 frames, and
"a safety check that fires on unrelated screens would licence clicking red
things at random". It then had to move into `Gate.wait_for_any`, because a
RUNNING TASK never reaches the ladder and so nobody answered the real prompt -
but a gate has no green check to scope against, and the scoping was simply
dropped.

**The replacement scoping is a positive reading of "this is a modal": the
PANEL.** A dialog is flat between its two buttons; a battlefield is not.

    the real revive dialog         colour std   2.8
    three combat/unknown frames    colour std  61.5 .. 65.4

A gate at 20 is an order of magnitude clear of both, and takes the reference
set from 4 false fires to **0 of 138** with the real prompt still detected.

**The general lesson, and it is not about dialogs:** when a check moves to a
new caller, its GUARDS have to move with it. The thing that made it safe was
never the shape test - it was the context it was asked in. Shape alone is a
coincidence waiting to happen, and the log line will sound confident when it
does.

## EUDEMON GARDEN — the boss ladder, and a second rotation for the hunts

Village -> `Hunting House` label -> submenu -> `Eudemon Garden`. A paged list
of bosses, each row carrying a name, `Level:N`, a RANK badge and an attempts
counter `x N`; the bottom of the panel has `Material Market` and `Battle`.

**The Hunting House sub-app is no longer stuck.** `docs/UI_MAP.md` recorded S8
as NOT OBSERVED, stalled at "Loading... 3%" - it loads fine now, and so does
the garden.

Fourteen bosses over three pages on this account:

    SS  Izo · Kyunoki's Right Hand & … · Mudo & Kyo · Kojima      Level:1
    C   Kamaitachi 10 · Hell Horse 20
    B   Kabutomushi Musha · Kinkaku & Ginkaku
    A   Thunder Eagle 40 · Mammoth King 50
    S   Oceans Queen 55 · Ghost Soldier 60 · Battle Angel 70 · Infernal Chimera

Exactly TEN are non-SS, which is almost certainly why the reference bot's
`EudemonBossSequence` is `Boss1..Boss10` - their ten are the permanent roster
and SS sits on top. Their list is positional with no names and their build is
a different private server, so nothing transfers: **there is no boss data to
port.** The CMMhero source was deleted, only their `config.json` survives, and
the SWF extraction is 129 PNGs plus a manifest - no strings, no
ActionScript. The roster is read off the screen.

### RANK IS A COLOUR

Measured medians over each badge's saturated pixels:

    rank   hue    S     V     px
    SS     120   255   196   ~4000
    B      101   176   143   ~3100
    A        5   213   233   ~2600
    S       24   153   246   ~2350
    C       39   146   143   ~2100

SS and B are the closest in hue (120 vs 101) and are separated by SATURATION
as well - SS is fully saturated where B is 176 - so neither test decides
alone. That matters here specifically: confusing them would either exempt a
farmable boss from the blacklist or let a time-limited one be skipped. All 14
rows read correctly.

**CORRECTION - EVERY READ RANK IS BLACKLISTABLE NOW, SS INCLUDED.** The rule
used to be "non-SS only", and it was right for the code that existed then: the
roster was harvested only as a side-effect of hunting, so a STALE entry could
quietly cost an event attempt nobody chose to give up.

The scan button removed that premise. The roster is rebuilt from the live list
on demand, a boss that leaves is RETIRED rather than deleted so it returns as
itself, and identity travels by fingerprint - so an entry cannot drift onto a
different boss. With the list current by construction the refusal protected
nothing and only took a decision away from the operator, who can see the event
bosses in the panel and knows which are worth an attempt.

`blacklistable()` is still the single place the rule lives, and it still
refuses an UNREAD rank (`None`): skipping something unidentified is a
different risk and stayed forbidden.

### THE LAP: RECRUIT, FIGHT, RETURN — and recruit BEFORE choosing a target

The operator's shape for a Eudemon lap: fill the party, enter the garden,
start a boss, win or lose, be back in the village, repeat.

**Recruiting belongs INSIDE the loop.** The recruit panel says so itself -
"Teammates will leave your group after each mission or boss" - so a party
filled once at the start is gone by the second fight.

**And it must happen BEFORE the target is chosen.** A first version recruited
after picking a target, then walked back into the garden and re-found that row
on PAGE 1 only. A target from page 2 or 3 would have kept a stale `y` and the
next click would have landed on a different boss. Recruiting first makes the
ordering irrelevant.

A failed recruit is NOT fatal. A party is help, not a precondition, and a boss
can be attempted solo - so it is logged and the hunt goes on.

### THE PANEL'S BLACKLIST — keys matched by fingerprint, never by hash

The dock lists every boss the hunt has seen (`SS-1`, `A-2`, ...) and the
operator clicks the ones to skip. Two decisions worth keeping:

* **Identity is matched with `same_row`, not by hashing the fingerprint.** A
  hash changes whenever any pixel does, which is the opposite of what a stable
  identity needs; `same_row` already compares with tolerance and is what the
  rest of the module uses. Verified: re-harvesting the same three pages adds
  nothing to a roster of 14.
* **Every boss is clickable, event bosses included** - see the correction
  above. The command defers to `blacklistable` rather than testing the rank
  itself, so there is still exactly one place the rule lives.

The roster is persisted, because the panel has to offer the bosses BEFORE a
hunt runs - an operator picks what to skip and then presses Run, not the other
way round.


### THE SCAN BUTTON, AND WHY IT SITS WITH THE LIST

**The boss list changes with events**, so which bosses exist is not a fact to
learn once. `EudemonScan` pages the whole garden and refreshes the roster
without fighting anything - scanning is cheap and reversible, fighting is
neither, and an operator who wants to see what is available should not have to
start a fight to find out.

Three decisions worth keeping:

* **It lives ABOVE the boss grid, not in the task row.** The scan is what
  FILLS the list beneath it; the two are one control surface, and an operator
  looking at a stale list should not have to know the fix lives elsewhere in
  the panel. It was in the task row first and that was a duplicate of the same
  command, with the copy sitting where its effect is invisible. `Task.hidden`
  keeps it runnable while absent from the row - so a command must be validated
  against `BY_KEY`, never against the panel's visible list, or the button that
  exists to run it cannot.
* **It runs on click** (`run_task`), because it answers a question rather than
  starting a shift. It will NOT interrupt: if something is already running the
  task is queued and says so, since aborting a mission to answer a question is
  the worst reading of that button.
* **A rescan REPLACES rather than adds**, because `harvest_roster` only ever
  adds and the panel would keep offering bosses an event has taken away.

**A retired boss is kept, not deleted, and a test caught why.** Dropping the
entry looks equivalent and is not: the FINGERPRINT goes with it, so when the
event returns there is nothing to match and the boss comes back a stranger -
new key, no name, and the operator's blacklist entry silently detached. The
first version did exactly that and passed, because `_next_key` reissued the
same numbers by coincidence. Entries now carry `listed: False`; the panel
shows only listed ones, and identity survives an absence.

### `x0` MEANS NO TRIES LEFT — but only when it actually READ

A row reading `x0` is skipped outright. `count_at` returns None where it could
not read, and **None is not zero** - the count digit merges with the panel
border at the threshold that isolates it, so unreadable is the common case.
The authority therefore stays `start()`: press Battle and ask whether the
screen moved. That costs one wasted click per exhausted boss per sweep and
needs no digit at all.

### A EUDEMON WIN IS A DIFFERENT PANEL FROM A MISSION SUCCESS

The first live boss was WON and the bot reported `stalled`. Measured on the
frame it stalled on (`ref/auto/eudemon/reward_panel.png` - `Izo`, XP 45,650 /
Gold 45,650 plus a materials drop):

    mission_success   0.266     <- the farm/TP banner does not match at all
    result_panel      0.524
    mission_start     0.668     <- the green check is not on this panel
    close_popup_x     0.951     <- the X that dismisses it, at (2132, 242)

A Eudemon boss pays out on a TALL PORTRAIT panel closed by a RED X, where the
farm and TP pay out on a wide banner closed by a GREEN CHECK. So `tp.close_out`
can never bank one - it waits out its 45 s looking for a check that is not
there, the turn gate times out at 90 s, and the runner calls a won fight
`stalled` while the reward sits on screen.

**The `Share` button on that panel must never be pressed** - it publishes to a
social feed, the same standing rule as the TP "Share with Teammates" dialog.
The X is located BY TEMPLATE and additionally constrained to the panel's
top-right corner, and the suite measures the Manhattan distance from the click
to the green Share control (1517 px) so a loose match cannot drift onto it.

**The garden's own close X is the same glyph in the same corner**, and
`close_popup_x` matched all three list pages. A reward panel is never the
list, so `reward_panel` returns None whenever `plates()` sees one - a positive
reading of the list rather than another threshold.

General shape, for the third time in this file: **a reward screen is not one
asset.** The green check alone is drawn at three sizes; now there is a
payout panel that does not use it at all. Check what the screen actually
carries before reusing a close-out.

### A RANK COLOUR IS NOT AN ANCHOR — locate the LIST first

The first version of `eudemon.rows` assumed the five row positions and read a
rank badge at each. It matched **60 of 113 non-garden reference frames** -
combat, the lobby, the village - so the hunt never called `to_garden`, paged an
imaginary list and reported "every boss is blacklisted or finished" from the
village.

Saturated art is everywhere; a hue window over a small box says nothing about
which SCREEN this is. The white NAME PLATES are structural and say it exactly:
measured 343x67 at x=1033, evenly pitched (153, 158, 158, 157). `plates()`
finds those and `rows()` reads ranks only where a plate actually is - 0 of 113
false positives, all 14 rows still read.

Two details that had to be measured rather than guessed:

* **the plate WIDTH varies with the boss name** (325..425, because a long name
  merges the plate with the art beside it), so height and x carry the test and
  the width window is generous;
* **a gap may be a MULTIPLE of the pitch** when one plate fails to segment, so
  multiples are accepted rather than widening the pitch window - which would
  let arbitrary pale bars through.

This is the same lesson as `looks_like_mission_scene` and the SS `stage_dialog`
firing on the lobby: a detector needs a POSITIVE reading of the screen it
belongs to, not merely the absence of a reason to doubt.

### THE COUNTER IS ADVISORY — Battle is the authority

`x N` reads `x1` for SS and `x3` for the rest today, and whether those are
daily maxima or today's remainder is UNKNOWN until a full cycle is watched.
Nothing depends on knowing, because exhaustion is decided the way
`tp.start_row` decides it: press Battle and ask whether the screen moved. A
boss with no attempts left cannot start a fight, so a still screen is a
POSITIVE reading of "finished" rather than an inference from a digit.

That is deliberate insurance: the count digit merges with the panel border at
the threshold that isolates it (measured 54x130 for a "1" whose true height is
~90), so the reader can legitimately return None, and None must never be
mistaken for zero.

### `tp.row_fingerprint` IS WRONG FOR THIS LIST

It samples x 1700..2500, which on a TP list is the row's own right-hand side
but HERE is the shared boss PREVIEW PANE - repainted on selection and
identical across the five rows of a page. Using it would hand back the same
fingerprint for every row, so ONE blacklist entry would silently skip the
whole page. `eudemon.row_fingerprint` samples the name plate (x 1033..1376)
instead; verified 14 rows, zero collisions.

General shape, and this file already records it for the character finder: a
helper that is right for one screen is not automatically right for another
that merely looks similar. Check what the coordinates actually land on.

### A SECOND SKILL ROTATION, FOR THE HUNTS

Bosses are a different fight from a story mission, so the panel keeps a hunt
skill order beside the main one and `battle_cfg(profile="hunt")` prefers it.
This is the arrangement the reference bot uses too - `HHSkill` and
`EudemonSkill` sit beside `LevelingSkill`, `CWSkill` and the rest.

**An empty hunt order falls back to the MAIN order, never to Attack-only.** A
boss fight with no rotation is the worst possible default, and an operator who
has not filled the second list in has not asked for one. The two lists live in
different files so neither can overwrite the other, and the panel's two slot
rows are built by one filler taking the command as an argument, so a slot
added to one cannot leak into the other by copy-paste drift.

### THE PANEL COULD ONLY OFFER PAGE 1 — the search stopped the harvesting

The blacklist UI was complete and the roster was nearly empty, so the feature
looked built and was unusable: the operator could not skip a single C, B, A or
S boss, which is the whole point of it.

`hunt` harvested the roster INSIDE its target search, and that search stops at
the first startable boss:

    for page in (1, 2, 3):
        found = goto_page(...)
        harvest_roster(...)      # only for pages actually visited
        ...
        if target: break         # page 1 always has a startable SS boss

Page 1 always carries the SS rows, so it broke out there on every lap and
pages 2 and 3 were never visited. Measured on the live roster file: **5
entries — SS-1..SS-4 and C-1** — out of fourteen bosses.

`survey_roster` pages the whole list once per hunt, separately from target
selection. Executed over the three committed garden pages it harvests all
fourteen, and all fourteen are offerable in the panel (the SS lock was later
removed - see the correction under RANK IS A COLOUR):

    SS-1 Izo · SS-2 Kyunoki's Right Hand · SS-3 Mudo & Kyo · SS-4 Kojima
    C-1 Kamaitachi · C-2 Hell Horse
    B-1 Kabutomushi Musha · B-2 Kinkaku & Ginkaku
    A-1 Thunder Eagle · A-2 Mammoth King
    S-1 Oceans Queen · S-2 Ghost Soldier · S-3 Battle Angel
    S-4 Infernal Chimera

**Once per hunt, not once per lap.** The roster is persisted and matched by
fingerprint, so a boss stays known once seen; two extra page turns on every
lap would buy nothing. An empty page ends the survey early - the list is
contiguous, so an empty one is the end and not a transient miss.

**And the test EXECUTES the survey.** A source-level assertion would pass
against a survey that cannot run - this suite has shipped that bug twice
(`arrow`, `play`). Its ordering check also strips COMMENTS as well as
docstrings, because the fix is now explained in a comment beside the call, and
a naive grep would match the prose rather than the call and pass with the call
deleted. That is the docstring trap wearing a different hat; the docstring
half alone has caught this suite four times.

## RECRUITING A PARTY — and the one place a resize is justified

Two party slots (`Team 0/2`), filled from the recruit rail. The operator wants
them filled because some Hunting House and Eudemon bosses are hard to solo.

**NPCs COST TOKENS AND ARE EXCLUDED, three ways.** This bot never spends
tokens, and the rail mixes NPC cards in among the friends even on the friends
tab, so a card is PROVEN a friend rather than assumed from which tab is open:

    card colour     friends S 33..47, NPCs S 104..117
    card structure  a friend card is exactly "Lv" + one or two digits; the
                    NPC cards break the motif (one has no digits, one carries
                    a 37 px blob of card art among them)
    BUTTON COLOUR   a GREEN + recruits a friend for free, a BLUE + is the
                    token-priced NPC

**The button colour is the one that earns its place.** On a paged rail two
NPC cards read DESATURATED and passed the colour test; the blue-button check
caught them, and `eligible` returned four names out of six. Neither test is
load-bearing alone, which is exactly why there are three.

**STRONGEST FIRST.** The rule is "at or below the player's level", and the
first version satisfied it by sorting ascending and taking the WEAKEST two.
The point of a party is help, so it takes the highest that qualify.

### THE + ROW IS BELOW WHAT RUFFLE DRAWS, and only a resize reveals it

The buttons sit at the foot of each card, past the bottom of the rendered
game. Everything cheaper was measured and none of it works:

    taller browser viewport   free (game rect and every anchor verified
                              identical at 800/860/900) but no help - the
                              clip is not the viewport
    overflow:visible on the   no reflow, no help
    clipping .site-wrapper
    shifting the game up      really moves it (iframe y 0 -> -59, and the
                              Admin Message band does disappear) but the
                              buttons stay absent at -59, -90, -120, -150
    clicking the 8 px sliver  the click lands and changes nothing

Only enlarging the player draws them - measured `player 960x839 -> 960x909`.
That is this file's oldest prohibition, so it is done as a BOUNDED exception:
grow the page wrappers, click, restore in a `finally`.

**The restore is verified, not assumed.** After a full cycle a control at a
known position was clicked and the rail responded both ways (mean |diff|
14.64), so click -> stage mapping survives. Worth noting the prohibition's
own premise has drifted: it was written after forcing 960x839 on a 960x720
stage, and the player is 960x839 today with clicking working perfectly - so
"taller than the stage" cannot by itself be the fault.

### TWO TRAPS WHILE FINDING THIS

**A green PAGING ARROW is not a `+`.** A loose sliver filter (35-80 px wide)
matched the rail's arrow at 51x76, clicked it, and PAGED THE RAIL - which
silently changed which friends were on screen and is why a Lv 42 player got
recruited during a test. The `+` discs are 84x69; the arrows are excluded by
requiring width >= 60.

**The friends' shield emblems are blue.** A first blue-button filter matched
them at 46x31 and vetoed real friends as "NPC columns". The `+` row is
separated by being at the very bottom and much wider.

### THE RAIL IS LOCATED, NEVER ASSUMED

The `Lv` badge row moves when the player is enlarged (measured y~1370 normal,
y~1440 grown), so a fixed offset from the button row found nothing at the
grown size and read as "no friends here" rather than as a geometry fault.
`find_rail_band` takes the densest run of amber glyphs instead. Cards are
grouped by GAP (16-22 px within a card, 133 between), not by the measured
192 px pitch, and the label/digit split is relative to each card's own tallest
glyph so it survives any scaling.

One digit set serves the card badges and the player's own larger plate: the
player's "8" at 34 px matched a card-harvested "8" at 25 px with d=0.109 and a
2.8x margin. An unread digit is REFUSED - a misread level could recruit
someone above the player, which is the one thing the rule forbids. `1` is
still unharvested and will refuse until seen.

## SS: THE OTHER TWO FAMILIES — Balance Control and Lights Out, both cleared

Both were sitting behind one obstacle that had nothing to do with either
puzzle, and neither had ever been seen.

### THE HINTS PANEL HAS NO X, AND ITS BUTTON IS DRAWN OFF SCREEN

`Balance Control` and `Sage Sealed Boxes` open on a panel of illustrated rules
instead of the rune family's rules popup. `open_puzzle` swept three X templates
across it and pressed nothing, so both missions were abandoned as unrecognised
and the families could never be learned. Measured on all five saved frames, no
X-shaped anchor comes close:

    close_popup_x      0.608 / 0.665      close_promo_x    0.466 / 0.454
    close_popup_x_menu 0.620 / 0.610      mission_start    0.598 / 0.502

The only exit is a wide green button at the bottom of the panel — **and it does
not fit on screen.** The game is 839 CSS px tall in a 720 px viewport, so its
bottom 238 captured px are hidden, and the button lands exactly there: tops
measured at y=1404 and y=1410 against a frame that ends at 1440, leaving a
30..36 px sliver. So `ss.hints_button` clicks near the blob's TOP rather than
its centre — the centre of a clipped button is off screen, and a Flash button's
hit area is the whole button, so the sliver is as good as the middle.

The positive signal is the WIDTH: both panels draw it at 412 px. The band floor
of 1350 is what does the real separating, because the same green range also
catches the puzzle art at y=1300 (884x43 and 440x101) and a width window alone
would let the 440 through. Verified: 5 of 5 hints frames, 0 of 94 other
reference frames.

### BALANCE CONTROL IS A SUBSET SUM, and the target is forced

Two columns of numbers, a circle button per row between them, a red SUM box
under each column, `Target: N` at the top, and a countdown. Clicking a row's
circle SWAPS that row's two numbers — verified by prediction, not assumed: the
first click was told which pair reversal to expect and the board was re-read to
confirm it.

Because a swap only moves values BETWEEN the columns, the grand total is
invariant, so the target is forced to half of it — measured on every board
seen (100 -> 50, 104 -> 52, 152 -> 76). Nothing therefore reads the white
`Target:` text: deriving it costs no perception and cannot disagree with the
board. With `d_i = right_i - left_i`, the puzzle is

    choose S with sum(d_i for i in S) == total // 2 - left_sum

Four rows is sixteen subsets; brute force is the whole algorithm, and `solve`
returns the SHORTEST set because boards often have two answers (stage 1's had
`{0,2}` and `{1,3}`).

**STAGE 2 HIDES THE SUMS — they read `??`.** So the sums are COMPUTED from the
rows and the printed values are only a CROSS-CHECK when present: a column must
equal its own sum box, which is exactly the invariant a misread digit breaks.
A reader that depended on them stopped dead on the second stage of every
mission. `?` is an EXEMPLAR rather than a fallback, so "deliberately hidden" is
read positively and never confused with "a digit I could not recognise" — the
two want opposite responses.

Digits are read from a mask, so **one exemplar set serves both the pale-yellow
row numbers and the RED sums** — the same trick the kekkai counters needed.
0 to 9 are harvested; measured worst distance 0.037 at a worst margin of 5.8x.

**ANCHOR ON THE BOXES, NOT ON THE SUM TEXT.** The first version keyed on the
red sum digits and died on stage 2's `??`. The boxes are what both variants
draw: near-black rectangles, interior grey 31 against a border of 77, 359x157
at a 203 px pitch, columns centred x=1236 and x=2188. A `grey < 40` pass finds
the bottom THREE rows on every frame held and never the top two — the glow
behind those is brighter — which costs nothing, because the bottom row plus the
pitch locates the rest.

`locate` answers "where are the boxes"; **`board_present` is what DISPATCH
uses**, and the difference is not cosmetic. Red text is everywhere in this game
— floating damage, status effects, the Admin Message banner — and the
sum-digit version fired on **31 of 94** combat and lobby frames. Requiring two
columns of evenly-pitched boxes with at least two paired pale rows above them
brings that to 0 of 99.

### THE CLOCK IS THE REASON `balance.py` COMPUTES INSTEAD OF POLLING

169 s per stage (99 s on the hints illustration). The first live look at the
board cost the mission outright: reading it, measuring the geometry offline and
coming back to click took longer than the timer, and the stage ended
`Mission Fail` at `0s` with nothing clicked. Every step is now one pass —
locate, read, solve, click — and the module never waits on anything it can
compute.

### A STAGE IS MANY BOARDS UNDER ONE CLOCK — and that produced a FALSE FAILURE

Balancing the columns does not end the stage: the game immediately deals a
FRESH set with a fresh target and the timer keeps running.

    169s   left 17 18  7 12 = 54   right  5 24 15  2 = 46   target 50
     94s   left 19  5  9 34 = 67   right 12  1  1 23 = 37   target 52

The first attempt read that second board as the result of its own clicks and
reported *"after the swaps the sums are 67/37, not 50"* — a false failure on a
round it had just WON. That is the vacuous-verification trap this file already
records for the hand-seal slots: a reading taken after the screen has moved on
says nothing about what we did. Note the target tracked the total both times,
which is the strongest evidence for the swap model — the total is only
invariant under swapping, and a re-roll is exactly where it may change.

So a round's verdict is "did the board become something OTHER than what my
swaps would have made it": that is a re-roll, and a re-roll only happens on
success.

**AND A BLINK IS NOT AN ENDING.** The first version called the board gone the
instant `locate` missed, and a re-roll redraws the boxes — so every round
returned "gone" after zero wins, which meant **a wrong answer and a win were
reported identically**. That is worse than a wrong count: it is the loss of the
only signal that says whether the solver is right. An absent board now has to
persist for the whole wait, and only the stage dialog ends a round early.

### LIGHTS OUT — a 3x3 GF(2) system, cleared in five presses

`Sage Sealed Boxes`. Nine spheres in a wooden frame and a timer; the game's own
hints panel gives the rules, half of them in Spanish: *"There are randomly 1-2
circles with light at the start"*, *"Extinguish all the light to clear the
stage"*. Lit is ON, grey is OFF, pressing toggles a neighbourhood, and over
GF(2) that is `A x = b` on nine unknowns. The 3x3 plus-rule matrix is
invertible, so every board has exactly one solution — verified against **all
512 states**, not a sample. Live:

    O........  ->  press [0, 2, 5, 6, 7]
    .O.O.....     ..OO.O...     ...OO...O     ....O.OOO     dark

Five presses, every intermediate state exactly as predicted, then
`Mission Success! Sage Sealed Boxes  10,000 gold / 10,000 XP`. It is a
ONE-STAGE mission.

**The rule is LEARNED rather than assumed.** The plus shape is the prior; every
press is read before and after, so it reports which cells it actually toggled
and the model corrects itself from moves that were going to be made anyway.
Nothing is spent probing — which matters, because this project has already lost
an SS mission to a diagnostic sweep that ate the budget it was diagnosing.
`solve` does not rely on invertibility either: it eliminates, then enumerates
the solution coset and returns the shortest member, so a learned matrix that
turns out singular still works.

**TWO OF THE NINE CELLS DEFEAT A CENTROID**, and both had to be measured:

    a LIT sphere      masks 232x222 against a grey one's 274x274 - its bright
                      core falls outside both colour ranges, so the centroid
                      lands 24 px up and left of the true centre
    the BOTTOM ROW    is clipped by the viewport at y=1440, masking 274x196,
                      so its centroid sits 36 px high

In both cases the LEFT and TOP edges survive and only the far side is missing,
so `left + R` / `top + R` — with R from the whole spheres on that very frame —
reconstructs all nine centres to within 7 px against a radius of 137. Measured
grid: columns 1382 / 1733 / 2085, rows 668 / 1028 / 1388.

**Requiring six whole spheres does not work**: with one light on only five are
whole, and two x clusters cannot say WHICH two of the three columns they are.
Reconstructing the centres first removes the ambiguity instead of guessing at
it. First attempt did the arithmetic on `top + bw//2` per blob and inherited
the lit sphere's shrunken width, putting the extrapolated bottom row at 1372
instead of 1388.

### A PUZZLE DRIVER'S VERDICT MUST NOT VETO `close_out`

Lights Out cleared in five presses, and `run_one` reported `banked=False`.
The driver had returned "lost" — no board and no stage dialog followed — while
`Mission Success! 10,000 gold` was on screen at that moment, because a
one-stage mission ends straight on the reward panel and the panel is not an OK
button, so `stage_dialog` cannot see it.

Two fixes, and the second is the general one:

* `ss.mission_over` tests for the reward panel, so a driver stops at once
  instead of waiting out its blank tolerance;
* **`close_out` is asked either way**, because it is the measurement that
  establishes a banked mission. A driver's opinion about its own stage is not
  that measurement. (The rune path keeps its veto; only the puzzle branch
  changed.)

Also: a stage ending is not instant. Both drivers tolerate a bounded run of
frames with neither board nor dialog (`BLANK_TOLERANCE`), and only a sustained
blank is a real loss.

### A STAGE DIALOG IS A SOLID BUTTON OF ONE FIXED SIZE

`stage_dialog` fired on the LOBBY. Found by peeking at a healthy village on a
fresh launch: `stage_dialog -> ('fail', (1585, 1062))`. Both SS puzzle drivers
check the dialog BEFORE anything else, so a false "fail" makes the bot click
that spot and abandon a mission that is still running.

Three gates, measured, and the first two do not suffice alone:

    village sign      337x121  aspect 2.79  fill 0.19   area  7,631
    character select  323x156  aspect 2.07  fill 0.65   area 32,686
    TP mission list   445x105  aspect 4.24  fill 0.88   area 41,212
    Stage Clear       351x108  aspect 3.25  fill 0.83   area 31,542
    Mission Fail      352x108  aspect 3.26  fill 0.79   area 29,891

FILL removes the village sign - an OK button is a filled rounded rect while
village art is outlines and lettering. That is the same solid-not-outline test
`find_confirm_point` needed, for the same reason.

WIDTH and HEIGHT remove the other two, and they are legitimate here because
the viewport is pinned, so the game draws this button at ONE size - the way
the command discs are one size. Aspect cannot separate 2.07 / 3.25 / 4.24
without being fitted to those three samples.

**The character-select case is the one that matters.** `Delete` sits beside
`Play`, which is the reason this project only ever clicks Play BY TEMPLATE; a
red blob passing as an OK button there is exactly the click-by-offset that
rule exists to forbid. The suite now asserts that no character-select frame is
ever read as a dialog, and that a HOLLOW button of the right size is refused.

Result: 0 of 102 reference frames read as a dialog, while both recorded button
sizes are still accepted at their measured centres.

**And note how it was found** - not by a failing test, but by pointing the
detector at a screen it should say nothing about. Scoring a detector on
frames it is supposed to REJECT is cheap and this project keeps finding real
faults that way.

### THE RUNE DRIVER NEEDED THE SAME ENDING THE PUZZLE DRIVERS GOT

Measured live, and it threw away a win:

    08:17:14  guess 6: Yellow,Black,Red,Green,Blue,White   (pool A=6 B=0)
    08:17:29  panel closed after guess 6 -> SOLVED
    08:17:29  SS: no puzzle and no dialog on screen - stopping rather than
              clicking blind
    08:17:29  SS: 1 stage(s) cleared, lost
    08:17:29  mission did not complete; it stays in the list and will not be
              retried this pass
    08:17:32  resume: mission_success (mission_success conf=1.000)

The stage was SOLVED and `Mission Success!` was on screen three seconds later.
`ss.play` fired in the gap between the two and called it lost, so `run_all`
recorded a failure about a mission it had just won. Only the resume ladder
noticed the reward panel and cleared it.

Balance and Lights had already been given a blank tolerance and a
`mission_over` check for exactly this; the rune family — the oldest and
best-tested of the three — was left without either, because the fix was
applied to the drivers that happened to be written that day. **That is this
file's recurring shape: a recovery path that exists in two places, only one of
which was taught the new trick.** The suite now iterates all THREE drivers
rather than naming the two.

The same edit removed the rune branch's veto on `close_out`, for the reason
already recorded there: a driver's opinion about its own stage is not the
measurement that establishes a banked mission.

### CORRECTION — COMPLETED SS MISSIONS DROP OUT OF THE DAY'S LIST

TP's do not: this file records that they stay listed and go GREY, and the whole
`start_row` "did anything change?" mechanism exists because of it. **SS behaves
the other way.** Measured across one afternoon: five rows over two pages, then
two rows on one page, then a panel reading `1 / 0` with no rows at all.

Consequence, and it was a real bug: an exhausted SS day looks exactly like a
list that never opened, and `to_ss_list` reported *"the SS list did not open"*
about a perfectly healthy panel — which would have `run_all` blame navigation
for having nothing left to do. That is the negative-definition trap again:
"no rows matched" is not evidence that the panel failed to open. The list UI
carries its own anchors and all three read **1.000** on the empty panel against
0.18..0.35 for anything row-shaped, so the panel's presence is now a positive
reading.

### A FIXTURE DIRECTORY THE BOT WRITES TO CANNOT CARRY A HARDCODED EXPECTATION

Second instance, after the cooldown frames. The hints test globbed
`ref/auto/ss/*.png` and asserted every frame offers a hints button; the moment
a live run saved a Lights Out BOARD into that directory, it asserted that a
board offers a hints button and failed on correct code. The fixtures are named
for what they are now — `hints_balance_*`, `hints_lights_*`, `lights_board*`,
`balance_board*` — and each test globs only its own.

### STILL OPEN on SS

* **A red dialog was followed by `Mission Success!`.** Stage 2 of Balance
  Control ended on the red OK button — which this file records as `Mission
  Fail` — with all four of its boards arithmetically correct, and then the
  reward panel banked the mission. So either that button is not always a
  mission failure, or a stage can be failed without failing the mission. The
  dialog is now SAVED to `ref/auto/ss/balance_fail_*.png` when it appears,
  because it is gone by the time an operator looks.
* **Hidden sums remove the arithmetic cross-check.** When stage 2 draws `??`
  there is nothing to validate a misread row against, and a wrong read is only
  caught afterwards by the board failing to re-roll. Reading the white
  `Target:` text would restore the check (`target == total // 2`) and needs one
  more exemplar set.
* **A failed mission still pays `close_out`'s 45 s timeout** looking for a
  reward panel that cannot be there.


## THE GAME'S OWN SETTINGS — and three attempts aimed at the wrong layer

The operator wanted to switch Ruffle's render backend from the panel, because
wgpu/WebGL is the lightest but shows graphical glitches. Three mechanisms were
tried and all three failed, each for a different reason, and all three looked
plausible while doing nothing:

    1. patching `RufflePlayer.config.preferredRenderer`
       -> lost: a PER-LOAD config overrides the global one
    2. wrapping the element's `load(options)` to inject the choice
       -> did nothing: the site calls `player.load(swfUrl)` with a bare STRING
    3. that prototype wrap
       -> never applied at all (`__nsbotRendererPatched` stayed false)

Measured throughout, which is what finally named the problem:

    RufflePlayer.config.preferredRenderer  = "webgl"       <- our override
    player.loadedConfig.preferredRenderer  = "wgpu-webgl"  <- what Ruffle used

So asking for `canvas` still produced a WebGL2 context, and the operator
correctly reported that the graphics had not changed.

**THE SITE ALREADY HAS A CONTROL FOR ALL OF IT.** A gear icon
(`button#render-toggle-button`, in `div.menu-controls` beside fullscreen and
the main menu) offers renderer, graphic quality and server. It was never seen
because **FOCUS MODE HIDES IT** - focus mode hides the game's siblings, and
`display:none` is set on the button and a parent. Its emulator page reads, on
every load:

    const defaultRender   = isIOS ? 'webgl' : 'wgpu-webgl';
    const savedRender     = localStorage.getItem('renderMode')  || defaultRender;
    const savedQuality    = localStorage.getItem('gameQuality') || 'high';
    var   selected_server = parseInt(localStorage.getItem('ns_server_index') || '0', 10);
    const render          = urlParams.get('render') || savedRender;

So a setting is **one localStorage key plus a reload**. No config patching, no
race with the site's own assignment. Verified live, and this is the first time
any attempt actually took:

    before:  loadedConfig wgpu-webgl,  context webgl2
    after :  loadedConfig canvas,      context 2d

**GENERAL LESSON, and it cost three failed attempts: when a site already has a
control for something, find ITS control before reverse-engineering the
runtime.** The operator naming the gear was the fastest step in the whole
investigation.

**NOTHING IS CACHED ON OUR SIDE.** The site stores and reuses these keys
itself, so an earlier `run/renderer.json` was deleted: a second copy would be a
stale duplicate of the truth, which is the same mistake as any other cached
belief about page state. The panel shows what the GAME has stored.

**A write is VERIFIED by reading the key back**, and after the reload the
setting is re-read and a mismatch is logged. An unverified write is exactly how
the previous attempt offered a switch that silently did nothing.

### Two measurement mistakes worth remembering

**The canvas CONTEXT TYPE cannot identify the backend.** Both `wgpu-webgl` and
`webgl` draw through a WebGL2 context, so it only separates GL from canvas2d.
The backend actually in use comes from `player.loadedConfig`. An early log line
read "site asked for webgl" while echoing OUR OWN override back from the global
config - if it had been trusted, the conclusion would have been that the switch
worked.

**`iframe.contentWindow.localStorage === window.localStorage` is FALSE for
same-origin frames** and proves nothing: each window gets its own Storage
object over the same store. A probe used it as an origin test and reported
"not same origin" about a frame whose scripts, storage and DOM were all being
read successfully. Verified properly by writing in one window and reading it in
the other.

### The server setting deserves a sharper warning than the others

`ns_server_index` is stored the same way, and the list is read from the game's
own `servers` array rather than hardcoded (measured: two entries, index 0 the
primary host and index 1 a CDN host). But where the renderer only changes how
pixels are drawn, this points the client at a different game HOST - and whether
a character exists there is not something this bot can determine. The panel's
confirm text says so explicitly, and no switch was ever performed to find out.

`gameQuality` is deliberately NOT offered yet: only its default (`high`) has
been observed, and offering values the site may not accept would be another
control that silently does nothing.
