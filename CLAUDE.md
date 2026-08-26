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

### seal_entry: recognised, and honestly NOT solvable yet

The hand-seal minigame (`Skill : N / 4`, three hearts, a named jutsu, two empty
slots, ten face-up seals) is detected reliably by its "Skill :" label — 1.000
positive against 0.268..0.348 everywhere else — and then declined. Why:

* the two slots are card BACKS. They are the empty INPUT, not a revealed answer.
* **CLAUDE.md's "revealed briefly after Start" hypothesis does not hold** at
  47 fps. Clipping the capture to the slot strip got 237 frames in 5.01 s (a 4x
  speedup over full-frame) and showed only a READY overlay then the training
  dummy. No reveal.
* the mapping is not in the client we hold: a string search of the shell SWF
  finds no jutsu names and no seal vocabulary, consistent with the existing note
  that per-skill data lives in a server-fed `SKILL_DATA`.

Ten seals in two ordered slots is **90** possibilities against **three** hearts,
and a miss also REROLLS the target jutsu — so attempts cannot even be accumulated
against one skill. Guessing just spends the lives.

**What would fix it:** a jutsu -> seal-pair table, harvested once offline. The
likely source is the game's own Jutsu panel. That is a separate job, not
something to attempt mid-minigame with three lives on the line.

### The minigame is hand-seal SEQUENCE ENTRY, not pair matching

* HUD: `Skill : 1 / 4` (four rounds) and three hearts (lives).
* A named target skill with icon (`Lightning Edge`, `Fiery Spike Wheel`, ...).
* **Two face-down slot cards** beneath the skill name.
* A row of **10 face-up hand seals** along the bottom, all visually distinct.
* `Start` button, centre.

Verified interactions:

* Clicking a seal fills the next slot left->right and **greys that seal out**.
  No heart is lost per click; evaluation happens on sequence completion.
* A wrong sequence costs **one heart** (3 -> 2), **rerolls the target skill**,
  resets all 10 seals and both slots, and leaves `Skill : 1 / 4` unchanged.
  So a miss costs a life but not progress.
* The 10 seals **stay face-up** - verified across 7.3 s of continuous observation,
  no flip-back. The choices are not the memorised element.

**UNRESOLVED: where the required sequence comes from.** It is not derivable from the
visible screen. Most likely revealed briefly in the two slots immediately after
`Start`; that window was missed twice by fixed-interval capture. If no reveal
exists, a skill -> seals table is required instead.

### Scroll family (memory board) — SOLVED live, mission banked

"Secret TP Scroll" completed by the bot: **Remaining Cards x0 with the hourglass
still at 85**, then Mission Success (Gold 2,000 / XP 2,000). `engine/cards.py`.

A 4x5 grid of 20 face-down cards, ten pairs, and **a countdown that is the real
opponent** — running it out ends the mission with "Sorry, you are not qualified
to receive this scroll." There is no opening reveal (a 26 fps burst over 8 s
caught zero change), so it is a genuine memory game.

**Cell state is read from AGGREGATES, never from spatial distance.** The backs
are the logo over animated flames, so at any instant every back looks different —
spatial distance from cell 0 to the others ran 0..127.6 on a frame where all
twenty were face-down. Measured bands, which is what `cell_state` uses:

| state | mean sat | mean val |
|---|---|---|
| back (animated) | 28.2 .. 30.1 | 123.5 .. 125.0 |
| face | 113.4 .. 201.9 | 96.9 .. 185.1 |
| removed (blank slot) | ~46 | **255.0** |

**Face matching is a 3x4 mean-HSV signature, NOT the reference bot's metric.**
`CardSolver.cs`'s grey+Canny dual metric was ported first and does not separate
this board at all. Calibrated against twenty real crops whose ten true pairs are
known (`ref/auto/tp/faces/`, committed as a fixture):

| metric | worst true pair | best NON-pair | verdict |
|---|---|---|---|
| grey+Canny (theirs) | 139.85 | 104.96 | **INVERTED** |
| mean sat+val | 3.39 | 1.50 | **INVERTED** |
| **3x4 mean-HSV (ours)** | **4.56** | **8.99** | 1.97x gap |

Confirmed independently on a live board: true pairs 0.29 / 1.07 / 1.70, nearest
non-pair 23.88 — a **14x** gap. Gate at 6.5. Inset is HARMFUL here (a 20% inset
drops the gap to 1.27x), so the crop is used whole.

**Mutual-best partner is VACUOUS on its own.** With two revealed cards each is
trivially the other's best, which is how a run reported "10/10 pairs" against the
game's own "Remaining Cards: x18". Require mutual-best AND the gate.

**Three bugs, each of which cost a mission:**

1. **The flip detected the face and never STORED it.** `seen` stayed empty, so
   `unknown_positions()` never shrank and the run re-flipped the same two cells
   for its whole 80 s clock — 831 reads, zero progress. Symptom from the outside:
   "it presses things but does not memorise them, and repeats cards it should
   already know."
2. **A cell that will not flip must leave the rotation** (`skipped`), and must
   NOT be counted as `cleared` — that inflates the score with cells the game
   never removed.
3. **A matched pair burns away in a SMOKE PUFF, and mid-puff both cells read as
   BACKS** — the same reading a mismatch gives. Judging on a snapshot called 14
   pairs wrong in the very run that cleared the whole board. The verdict is
   therefore **asymmetric**: REMOVED is terminal and believed at once, BACK must
   HOLD for `MISMATCH_HOLD` (1.2 s) before it counts. As a backstop, a flip that
   finds a cell already REMOVED banks it, so a wrong rejection costs one click
   rather than poisoning the board.

**Trust the game, not the metric.** Every pair is adjudicated by watching the two
cells; the metric only proposes. Rejected pairs are remembered so they are never
proposed twice, and both faces stay in memory for their real partners.

### Speed: what actually made the timed board winnable

The board went from timing out to finishing in 33.9 s of an 85 s clock. Three
costs, in order of size:

1. **Full-frame capture is 168-173 ms (5.9 fps).** Clip to the board and it is
   50 ms (20 fps); the template gate on the clip is 12.5 ms against 72.3 ms on a
   full frame. **63 ms per read against 245 ms — 3.9x.**
2. **Fixed sleeps.** A guessed `flip_settle=0.75` on every flip became a poll
   that returns the instant the cell shows a face.
3. **Human-like click pacing.** `Actor`'s defaults sleep 0.18-0.55 s before and
   0.4-1.1 s after EVERY click — up to 1.65 s each, ~40 s over a game. Tightened
   for the duration of a timed minigame and restored afterwards. Pacing is
   anti-detection cosmetics; on a countdown it is just a way to lose.

**CDP clip geometry has two traps, and both fail SILENTLY** — a mis-clipped frame
still decodes, has plausible dimensions, and reads confident nonsense:

* **A clip is DOCUMENT-relative; a full frame is the VIEWPORT.** The game page
  sits at `scrollY=301`, so a clip computed from viewport pixels lands 602
  captured px off. Adding the scroll offset took the difference against the same
  region of a full frame from a mean of 76.50 to **exactly 0.00**.
* **`scale` MULTIPLIES the device pixel ratio, it does not replace it.** At
  dpr 2 a 600x452 CSS clip returns 1200x904 at scale 1 and 2400x1808 at scale 2.
  The correct scale is 1.

Use `Capture.clip_for`, which does both. Verify any new clip by differencing it
against the same crop of a full frame — that is the only check that catches this.

**Do not poll flat out.** Capture runs at ~20 fps and every
`Page.captureScreenshot` forces the WebGL canvas to re-composite, which makes the
game visibly FLICKER for anyone watching. `POLL_INTERVAL = 0.10` gives ~8
reads/second, which is comfortable to look at and far more than fast enough — the
board was solved with 51 s to spare.

### TP list navigation — measured

* The mission is chosen **by row title template**, never by row position.
  `tp_scroll_row` 1.000 / 0.608 worst negative; `tp_scroll2_row` 0.992 / 0.533.
  The 0.6 worst negatives are each other, which is exactly the confusion to avoid
  — "Secret TP Scroll" and "Another TP Scroll" are different missions.
* **The TP list is a DAILY list and it SHRINKS as missions are completed.**
  Measured: after finishing "Secret TP Scroll" and "The Kekkai in the Forest" the
  list went from 5 entries over 2 pages to 3 entries on one page (1/1), with both
  completed missions simply gone. A picker that knows one mission per family
  reports "not on this page" as soon as that one is done for the day, so
  `tp.ROW_TEMPLATES` maps a family to ALL of its missions.
* **Mission Success can be covered by the "Share with Teammates!" prompt**, and
  none of the four existing X templates matched it (close_popup_x 0.719,
  close_popup_x_menu 0.586, close_promo_x 0.465, back_arrow 0.402) — close-out
  timed out with the reward unbanked. `close_share_x` (1.000 / 0.784 worst) fixes
  it. The prompt must be dismissed BEFORE the success check is searched for,
  because its "Share to wall" button carries a green check glyph of its own that
  scores 0.708. **Never click that button** — it posts publicly.

### TP geometry and capture notes

At viewport 960x839 / dpr 2, in CSS coordinates:

* 10 seals, ~148 px pitch, centres x = 147, 221, 295, 369, 443, 517, 591, 665,
  739, 813 at y = 540
* slots ~(447, 412) and ~(521, 412); `Start` (488, 200)
* **Frame-differencing does not work on this screen.** The training dummy animates
  continuously, so every frame differs by ~4000 px regardless of events. Read
  content, never deltas.
* Capture ceiling measured at ~82 ms/frame (~12 fps) over CDP.

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
