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
* Sessions persist in the browser profile, which is the credential store — the bot
  never handles a password. Server-side expiry should surface to the human, not be
  auto-recovered, since it may mean a password change or ban.
* Cold SWF load ~25-30s; warm ~8s. Loading-state timeouts must span that range.
* Known stalls on this server: the Hunting House sub-app hangs at "Loading… 3%".
* The game console is noisy and prints `Out :: Error :: Main :: initButton` lines
  that are **not errors** — a log-scraping health check would false-alarm.

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
* **Targeting hit-areas differ and this is UNRESOLVED.** `Attack` + click on the
  enemy NAME PLATE dealt damage (verified -7.7pp). Skills appeared to work when
  clicking the enemy SPRITE earlier, but later skill+sprite and skill+plate both
  produced no damage. Cause unknown. Investigate before trusting skill automation.
* **Status effects** seen: `Blood Feed (1)`, `Strengthen(1)`, `Blind(1)` - named
  red text with a stack count. Damage numbers render as large floating white text.
* **Mission flow varies between missions.** #1: cutscene -> traversal -> combat.
  #2: cutscene -> **loading** -> combat (no traversal), then traversal later.
  Branch on observed state; never follow a fixed script.
* The `Loading...` interstitial resolves normally (0% -> done). The Hunting House
  hang at 3% was specific to that sub-app, not a general loading defect.

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
