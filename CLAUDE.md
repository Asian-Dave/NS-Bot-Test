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

Digit exemplars are still harvested by hand as new renderings appear (`0` now
has 9 variants, `1` three, `2` two — outline AND solid — and **3+ none at all**). A glare-cleanup
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

## Cross-reference: CMMhero NS Bot (decompiled, `ref/tp/cmmhero`)

Third-party Windows/C#/Adobe-AIR bot for a **different** private-server clone.
Mechanics reference only - never run the binary (it hardware-fingerprints, plants a
DPAPI licence file that survives uninstall, and opens a plaintext WebSocket to a
hardcoded IP). Findings that change our design:

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
