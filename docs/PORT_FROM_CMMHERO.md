# Porting the reference bot: C# -> Python, symbol by symbol

Audience: a session that has been reading `ref/tp/cmmhero/src` and wants to know
what of it now exists on our side, what deliberately does not, and where the
seams are.

Read `ref/tp/cmmhero/CLAUDE.md` first for how to navigate the decompiled C#, and
`ref/tp/cmmhero/NOTES.md` for what that bot actually is. This file is only about
the translation.

**Everything below was validated against recorded frames in `ref/combat/` and
`ref/raw/`.** `tests/test_battle_stack.py` is 47 checks and all pass; run it
before and after any change here. Numbers quoted as measured are measured — if
you disagree with one, re-measure it rather than adjusting it to taste.

---

## The one thing to understand before anything else

**Their coordinates cannot be ported, and the reason is structural, not fixable.**

`FormMain.cs` holds ~2,500 absolute pixel constants — `skillPositions["S1"] =
(225,374)`, `T1 = (353,169)`, `PixelFound(447,341,"A72102")`. That works because
that bot *forces* the game window to one size (`SetParent` + `SetWindowLong`,
reparenting the AIR window into its own UI). Exactly one geometry ever occurs, so
absolute constants are safe and a single-pixel exact colour probe is safe.

We have neither guarantee:

| | reference bot | us |
|---|---|---|
| host | AIR standalone, window forced | Ruffle in Chrome, canvas varies |
| client area | ~800x440, fixed | 960x839 CSS, and capture scale varies |
| observed scale spread | none | **0.46 vs 0.545 — 18%** |
| perception | exact single-pixel equality | template match + colour masks |
| input | `PostMessage(WM_LBUTTONDOWN)` | `Input.dispatchMouseEvent` |

Two independent capture sets in `ref/combat/` put the same command bar at scale
0.46 and 0.545. Copying their numbers misses every slot. Worse, it misses
*silently* — you get a bot that clicks empty background and reports nothing wrong.

So the coordinate tables became **anchor-relative offsets**, and that is the
single largest structural difference between their code and ours.

### The invariant that makes it work

Locate the command bar; that also yields the scale. Then:

```
captured_px = anchor_px + offset_in_template_units * observed_scale
```

Validated by deriving offsets from the 0.46 frames and *predicting* the 0.545
frames: **8/8 target ring slots landed on their real borders** (confirmed by
border colour), skill slots within ~6px of borders found by an independent
projection profile.

The two geometries are a **pure uniform scale** — command-bar pitch divided by
match scale was 108.7 and 108.3, agreeing to 0.4%. That is why one scalar is
enough and no aspect correction exists anywhere in `geometry.py`. If you ever
measure a third geometry where that ratio moves, this whole model needs
revisiting and the tests will tell you.

---

## Symbol map

| reference bot (`src/`) | ours (`engine/`) | fidelity |
|---|---|---|
| `PixelLoop(List<(x,y,colour)>, timeout) -> int` `FormMain.cs:14602` | `gate.Gate.wait_for_any(conditions, timeout) -> Fired` | **faithful, generalised** |
| `PixelFound/PixelNotFound` `14548`/`14562` | `gate.pixel(...)` condition | faithful |
| `PixelSearch.Normal` `PixelSearch.cs:169` | `gate.pixel(tolerance=0)` | faithful |
| `GetPixelFromBackgroundWindow` `PixelSearch.cs:417` | `Capture.frame()` (CDP screenshot) | different mechanism, same property |
| `ClickAt(x,y)` `14985` | `act.Actor.click_pixel` | faithful |
| `ClickAt(x,y,colour)` `15002` | *not ported* — see below | gap |
| `ClickAtDelay` (SetCursorPos+SendInput) `14948` | *not ported* | not needed |
| `MoveMouse` (WM_MOUSEMOVE park) `14934` | *not ported* — see below | gap |
| `skillPositions` S1..S8 `2629` | `geometry.SKILLS` | **re-derived, not copied** |
| `skillPositions` T1..T8 `2629` | `geometry.TARGETS` | **re-derived, not copied** |
| `skillPositions` AT/CH/DO/RN | `geometry.COMMAND` | re-derived; it is a 2x2 block |
| `Ui[...]` anchors `2306` | *not ported* — see below | gap |
| `CheckSkillCD` (stubbed `return true`) `6904` | `battle.SkillRotation` + `combat.CooldownTracker` | **improved** |
| `skillPositionsCapture` (per-slot rect + colours) | `geometry.slot_box()` | shape ported, signal unknown |
| skill-queue demotion `~7115` | `SkillRotation.resolved/failed` | faithful |
| `Global.ClosingUsername` closing action | `battle.closing_action` | faithful |
| `Global.RelogAfter` | *not ported* | worth having |
| `FormMission` (3,862 lines) | `mission.MissionRunner` | **rewritten, not ported** |
| `Global.BotLoopDelay = 25` | `timing.poll_interval` (0.25s) | deliberately 10x slower |
| licence / WebSocket / DeviceID | *never* | out of scope |

### Ported faithfully: the state gate

This is the piece worth taking wholesale. Their entire control flow rests on it,
and there is not a single fixed sleep in their battle path. Ours returns a
`Fired` object rather than an `int` index, carrying *which* condition fired and
its payload — so a template condition hands back the match, and therefore a
click point, instead of just "yes".

Generalised in one way that matters: a `Condition` is a **predicate**, so one
gate can mix template matches, pixel probes and arbitrary callables. "Wait for
the Victory panel OR the command bar OR a loading screen" needs two template
checks and a colour read, and the caller should not have to care which is which.

Ordering is load-bearing and inherited from their design: conditions are
evaluated **in order** each poll. The Victory panel draws *over* the command bar,
so a gate listing the bar first would report "my turn" on a finished fight.

### Improved: cooldowns

Theirs is abandoned — `CheckSkillCD` is `return true` (`FormMain.cs:6904`), with
the whole calling convention and the `skillPositionsCapture` table left in place
around the stub. Their actual mechanism is queue demotion: a resolved skill goes
to the back of the array, so it cannot recur until everything else has been
tried. Zero calibration, and genuinely clever.

We keep that as the **fallback** and put `CooldownTracker` in front of it. The
game's own client decrements cooldowns once per round (`nextRound()` ->
`reduceSkillCooldown(1)`), so cooldowns are exact small integers, trackable by
bookkeeping. Known cooldown -> skip the slot; unknown -> offer it and demote
after it fires. `SkillRotation.candidates()` is where the two meet.

### Improved: the unwinnable fight

They read no HP anywhere in the battle path, and their only failsafe is a
wall-clock "stuck" timer. That timer can never fire on the case we actually
observed — enemy HP going `50.7 -> 43.0 -> 43.0 -> 43.0 -> 47.2` percent, i.e.
back **up**. The screen keeps changing, so a time-based check sees a healthy
fight forever. `combat.DamageWatchdog` counts turns since a new *low* and takes
`Run`. Tested against that exact recorded sequence.

### Rewritten, not ported: the mission flow

`FormMission` is a linear script. Ours cannot be, and CLAUDE.md says why
outright: mission #1 ran cutscene -> traversal -> combat, #2 ran cutscene ->
loading -> combat with no traversal, traversal later. So `MissionRunner.run()` is
a **dispatch loop** — capture, classify, do the one thing that state calls for,
repeat. The mission's shape is discovered as it happens, and an unexpected popup
is handled by the same machinery rather than derailing a script.

Two facts from their code and ours that the loop encodes explicitly:
* Mid-mission Victory panels show XP 0 / Gold 0. Normal. Only `mission_success`
  may increment a success counter.
* Battle count is not the traversal node count — "The Criminal Gathering" took 7
  battles showing 3 nodes. `max_battles` is a runaway guard, not an expectation.

---

## Gaps worth closing (their code has these; we do not)

1. **`ClickAt(x, y, colour)`** `FormMain.cs:15002` — clicks only if the expected
   pixel is present at the target first. A cheap guard against clicking a moved
   or dead button, and the generalised form of our "whitelist Play by template,
   never click by offset" rule. Our `Actor.click_pixel` has no such check.
   Adding it means a pixel probe or a small template re-verify immediately before
   the click.
2. **`MoveMouse(0,0)`** `FormMain.cs:14934` — after every click they post a
   `WM_MOUSEMOVE` to park the in-game cursor away from the button, so hover art
   does not linger and corrupt the next pixel probe. We do not do this, and our
   frames are captured right after clicks. Worth testing whether hover state is
   polluting any of our template scores; `Input.dispatchMouseEvent` with
   `type: "mouseMoved"` is the equivalent.
3. **`Ui[...]` single-pixel anchors** `FormMain.cs:2306` — `Village
   (46,90,#003A8F)`, `Loading (368,273,#3C1D5C)`, and four more. One colour probe
   on solid chrome instead of a template. CLAUDE.md notes we lack a cheap
   positive lobby anchor and that village labels are unusable. Our `lobby_rail_fortune`
   template works, but a pixel probe would be far cheaper on a hot loop. Needs
   our own coordinates — theirs are for an 800x440 client.
4. **`Global.RelogAfter`** — they relog every N battles. Long-session hygiene we
   have nothing equivalent to.

---

## Research questions this port opened

Ordered by what unblocks the most. First two need a live client; the rest are
answerable from the decompiled C# you already have.

1. **Does clicking a ring slot actually select that target?** The ring's
   *existence* on our client is measured (8 slots, upper four red-bordered =
   enemy, lower four yellow = ally; border pixel counts 254..667 for a drawn slot
   vs 0..11 for background). Its *clickability* is inferred from their bot. This
   is the single largest unverified assumption in the port. `battle._take_action`
   is the one place to change if it is wrong. Set `battle.click_target: false` in
   `Configs/mission.json` to test action-only behaviour as a control.
2. **Can an HP bar be mapped to a ring slot?** `combat.find_enemy_bars` returns
   bars by *y*, not by slot, so `target_policy: "lowest_hp"` cannot be honoured
   and currently degrades to `"first"` — see `battle._choose_target`, which says
   so rather than pretending. Closing this needs the geometric relationship
   between a ring slot and the name plate / bar of the combatant in it.
3. **What does `#000002` detect inside a `skillPositionsCapture` rect?** Their
   dead cooldown detector, e.g. `S2 -> (256,359,282,386,["000002"])`. Hypothesis:
   the near-black outline of the cooldown digit. If right, that is a cooldown
   signal needing no per-slot baseline — simpler than our `SlotBaseline`, which
   exists because global saturation was measured unusable (56..191 continuous, a
   pale slot reading 56 while fully ready). `geometry.slot_box()` already gives
   you the rect on our geometry; go look for a near-black glyph in it on a frame
   where a slot is known to be cooling.
4. **`PixelLoop2` vs `PixelLoop`** `FormMain.cs:14718`, `14801` — the second
   takes `captureSize` defaulting to 5. If that is a tolerance/neighbourhood
   variant, it is how they cope with pixels that jitter, and it tells us whether
   exact matching survives on animated art. Directly relevant: `gate.pixel()`
   has a `tolerance` parameter defaulting to 0, and we do not yet know when a
   non-zero value is legitimate versus papering over a bad probe point.
   Caveat from `NOTES.md`: a sibling function `PixelLoop2` at `14801` was already
   found NOT to be a tolerance variant — it races conditions concurrently via
   `Task.WhenAny`. Check which of the two overloads is which before concluding.
5. **`FindPixelColorRange` / `FindAllInRange` callers** `PixelSearch.cs:661/698/707`,
   `308`. They return *lists* of matching points, which is the shape of a
   fill-ratio measurement. `NOTES.md` records `FindAllInRange` as having no
   callers outside `PixelSearch.cs` and `FindPixelColorRange` as having one thin
   wrapper — but confirm that properly, because if any caller measures a bar
   rather than locating a button, it is a cheaper route to what
   `combat.bar_fill_ratio` does.
6. **Their mission list and grade handling** — `FormMission.cs` plus
   `Global.MissionList`. Cross-check against CLAUDE.md's hand-measured facts
   (Grade A = 7 pages, Grade C = 11, 3 rows per page, locked rows greyed with a
   padlock). A disagreement means we measured different servers, which is worth
   knowing before trusting either.

---

## Using it

```bash
# what is missing, and how to cut it. Needs no browser.
.venv/bin/python engine/run_mission.py --preflight

# the invariants. Run before and after any change.
.venv/bin/python tests/test_battle_stack.py

# dry run: classifies states and logs intent, clicks NOTHING
.venv/bin/python engine/run_mission.py

# live, once preflight is clean and the config is filled in
.venv/bin/python engine/run_mission.py --live --repeat 5
```

`--preflight` currently names **9 missing templates** with cutting instructions,
and `--live` refuses to start while any are missing. Two to note:
`mission_locked` is a hard safety requirement (without it the bot dead-loops on
an inert above-level row), and `tpl/click_to_continue.png` exists but must be
**re-cut** — measured 0.642..0.849 across unrelated states, false-fires on
combat, and made every frame classify as "cutscene".

Three config values are deliberately left unset rather than guessed:
* `mission.grade` — no default. Grade C page 1 gives 20 XP, Grade A page 1 gives
  4,870. Guessing wastes the run.
* `mission.traversal_click` — `null`. Encounters trigger on movement, so the
  mission stalls silently without it. A guessed click on an unrecognised screen
  is how you end up hitting Delete next to Play.
* `battle.rotation` — `["AT"]` only. Attack is the sole action measured to deal
  damage on our client (~8pp/hit). Slots are TYPED, not uniform: right-bank slot
  1 applied `Strengthen(1)` to self for 50 CP, zero damage, and costs ranged ~10
  to ~100 CP. Add `S1..S8` only after measuring each, and record what you find in
  `battle.slot_kinds`.

## Things not to undo

Each of these looks like it could be tightened, and each is the way it is because
of a measurement:

* **`min_conf=0.70`** in `BattleGeometry.locate`, not 0.85. The discrimination
  matrix in CLAUDE.md was measured on ONE geometry; on the 0.545 set the real
  command bar only reaches 0.746/0.788. An 0.85 gate classified every
  boss-encounter frame as "not combat". Measured separation: bar present
  0.746..0.949, absent 0.407..0.470.
* **Gating on `charge_btn` + `dodge_btn`, never `attack_btn`.** Attack peaks at
  0.791 and is the weakest of the four.
* **The pitch cross-check** in `locate()`. It is what rejects the epilogue
  frames, where both templates *do* match, 340px apart at mismatched scales. Drop
  it and cutscenes become combat.
* **`_find_all` sweeps scale.** Matching at native template size found zero
  copies of a button plainly on screen. Without the sweep a mission list reads as
  empty and the runner pages forever.
* **`_observe_progress` returns `"continue"` when no bar is found.** A missing
  measurement must never be able to trigger an abort. Do not feed the watchdog a
  zero.
* **`poll_interval` 0.25s, not their 0.025s.** Our capture is a full CDP
  screenshot at ~82ms; theirs is a local `GetPixel`. Polling at their rate would
  saturate the loop. Use `clip` (added to `Capture.frame` / `cdp.screenshot`) to
  make a hot gate cheaper before lowering this.
