# NS Bot

UI-level automation for Ninja Saga, a Flash game running on Ruffle. The bot looks
at rendered pixels and clicks. It does not touch game memory, network traffic or
the server protocol.

**Status: farms story missions, TP training, SS training and the Eudemon
Garden boss ladder unattended.** Grade selection, mission choice, dialogue,
traversal, combat and close-out all run without help; all three TP minigames
and all three SS minigames are solved end to end; and the Eudemon hunt fills a
party, fights a boss and returns to the village on a loop.

Measured, in single sessions with no intervention:

| | |
|---|---|
| story missions | 18 banked back to back, ~172 s per cycle |
| TP training | 5 of 5 banked, the day's list finishing empty |
| SS training | 5 of 5 banked — two combats, a rune, a balance, a lights |
| Eudemon Garden | 8 bosses banked in a row, recruiting a party each lap |

---

## Quick start

**Double-click a launcher.** It creates the virtual environment and installs the
one dependency on first run, then starts the bot.

| | |
|---|---|
| macOS | `Start NS Bot.command` |
| Windows | `Start NS Bot.bat` |
| Linux | `NS Bot.desktop` — run `start-ns-bot.sh` once first and it fills in its own path |

The launchers keep their window open on exit, including a failure, so a
double-click that goes wrong leaves something to read rather than a window that
flashes and vanishes. They append to `run/app.log` rather than overwriting it,
so double-clicking while an instance is already running cannot destroy the log
of the one doing the work.

Or from a shell:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python engine/app.py            # launches, or reuses a live browser
.venv/bin/python engine/app.py --attach   # refuse to launch; attach or fail fast
```

Neither the launchers nor a bare `app.py` need to be told which case they are
in: `browser.launch(reuse=True)` attaches to a browser already serving CDP and
starts one only when nothing is.

That opens a window with the game on the left and a control panel on the right.
Sign in once — the browser profile keeps the session.

**Prefer `--attach` whenever the browser is already up.** The site's session
cookie is a browser-session cookie, so quitting Chrome signs you out.

Only one dependency: `opencv-python-headless`. The DevTools client, browser
launcher and control plane are standard library.

## Which browser

**Any Chromium-based browser works** — Chrome, Chromium, Edge, Brave, Vivaldi,
Opera, Arc. The requirement was never Chrome; it is CDP, which is Chromium's own
protocol, and nothing here is Chrome-specific. Verified live against Microsoft
Edge (Edg/152): evaluate, the device-metrics override, screenshot capture, the
injected binding and mouse dispatch all behaved identically. The first one
installed is found automatically; `--browser /path/to/binary` (or `target.browser`
in the config) overrides it, and the log says which was used.

On Windows this is close to free — Edge is preinstalled.

**Firefox and Safari do not work**, and this is not something a path can fix:

| | protocol | why not |
|---|---|---|
| Chromium forks | CDP | what the bot speaks |
| Firefox | WebDriver BiDi | its CDP shim was always partial and is being removed |
| Safari | WebKit Inspector Protocol | different protocol, driven through `safaridriver` |

Either would mean writing a second transport for capture, input and script
injection — a rewrite of `cdp.py`, `act.py` and `capture.py`, not a setting.

## The bot window

The browser is launched with `--app=`, which drops the tab strip, omnibox and
bookmarks bar (measured: window chrome 274px → 90px). The panel is injected into
the game's own page, in the ~385px of wallpaper gutter beside the canvas, so the
game renders natively — there is no streaming anywhere in the viewing path.

It is worth being clear about what this is *not*. The game cannot be embedded in
some other runtime: Ruffle is WASM + WebGL, the session lives in this Chrome
profile, and CDP is how we click. The reference bot's "native" shell is Adobe AIR
+ CEF — a Chromium in a frame. This is the same trade without the extra runtime.

The panel offers:

| | |
|---|---|
| **Task** | resume to lobby, TP training, SS training, Eudemon hunt, farm missions, exam rune puzzle, idle |
| **Run** | run / pause / relog / stop, and quit |
| **Stop** | aborts the task, clears its progress, and relaunches attached — no terminal trip |
| **Quit** | removes the panel and exits for good |
| **Farm target** | grade (auto, S, A, B, C) and mission (highest, or a pinned page/row) |
| **Skill order** | click `AT CH DO S1..S8` to build a priority order; Attack is the floor |
| **Hunt skill order** | a *second* rotation used only by Eudemon/Hunting House fights; empty falls back to the main order, never to Attack-only |
| **Eudemon bosses** | every boss the scan has seen, by name — click one to skip it |
| **Scan bosses now** | pages the whole garden and refreshes that list; runs on click when nothing else is |
| **View** | focus mode, window size, renderer and server |

Farm target and skill order take effect on the **next** mission and persist to
`run/`, outside the tracked config. If no bot is attached the panel says so
rather than sitting there looking broken with dead buttons.

Focus mode is not decoration. The page scroll drifts, and the game is 839 CSS px
tall in a 720 px viewport, so which 119 px are hidden depends on where the page
happens to be scrolled. That caused "could not find the Special tab" on a healthy
Mission Room and ladder halts on screens it knows. Hiding the game's siblings
lets it reflow to the top so `scrollY` is 0 and stays 0.

## How it works

```
CDP capture → template match → state classify → policy → click (CDP)
```

* **Capture** is `Page.captureScreenshot`, not screen grabbing. No
  screen-recording permission, and the window need not be frontmost.
* **Clicks** are `Input.dispatchMouseEvent`, which reach the Ruffle canvas as
  trusted events. `pydirectinput` is not used and cannot be — it is Windows-only.
* **The game is a single canvas.** There are no DOM elements inside it, so
  template matching is the only interface available.
* **Waiting is always a gate, never a sleep.** A turn is resolved when the
  command bar disappears and returns, so a long skill animation simply delays the
  gate. Clicks issued during the enemy's turn are silently discarded by the game,
  which is why sleeping is the wrong tool.

## What it can play

**Story missions.** Reads the grade panel by colour (A hue ~103, B ~51, C ~128; a
locked grade renders grey and never reaches saturation), pages forward to the
first padlock and takes the last unlocked row before it, then plays the mission:
dialogue, traversal, combat, and the Mission Success close-out. A grade that
runs out of pages before showing a padlock stops on its last page instead.

In combat, a skill that is cooling is **not clicked at all** — on wgpu, where
the game draws a cooling tile in true greyscale (measured saturation 0.0 inside
the tile against 164.0 for a ready one). webgl does not apply that grey-out, so
there the reading is *unknown* and the skill is tried; see the renderer section.
Cooldown lengths
are also learned by bracketing them from outcomes, since the game does not
expose them. Enemies are found by MOVEMENT rather than colour, which cannot be
fooled by scenery — with the exception of *animated* scenery, so a mover that
stays unreachable is eventually treated as scenery too.

**TP training.** Missions are chosen by position and the minigame is identified
from the screen, not from the mission name. The pass has no mission cap: it
keeps taking startable rows until the list offers none. A completed row greys
out and stops being detected, so only *failures* are remembered — and by a
fingerprint of what the row says, because the survivors reflow upward into the
slot a finished mission vacated, and a remembered position would then name a
different mission entirely.

| minigame | state |
|---|---|
| Kekkai (rune Mastermind) | solved — missions banked |
| Scroll (memory board) | solved — cleared 20/20 with 51s to spare |
| Potion (hand-seal memorisation) | solved — five levels including an eight-sign round |

**SS training.** Five missions a day at five times the TP reward, in three
families — and, like TP, the family is read off the screen rather than from the
mission name, so one pass can cover three different minigames without being
told which is which.

| minigame | state |
|---|---|
| Sage Power Seal (rune Mastermind, multi-stage) | solved — stages escalate 5 → 6 runes, length read per stage from the seal's node count |
| Balance Control (subset sum) | solved — the target is *derived* (half the invariant total), so a hidden `??` sum costs nothing |
| Sage Sealed Boxes (Lights Out) | solved — 3x3 over GF(2), verified against all 512 states |

Two things make the rune family harder than its TP cousin, and both are about
the budget: a stage allows **ten guesses** and running out fails the *mission*,
not the stage. So the solver never spends a guess probing, and it resumes from
the rows already on the scroll rather than restarting.

**Eudemon Garden.** A boss ladder reached through the Hunting House. Each lap
recruits a party, enters the garden, fights, and ends back in the village —
because teammates leave after every boss, so a party filled once is gone by the
second fight.

* **Bosses are read off the list**, not configured: 14 on this account across
  three pages, with rank from the badge colour (SS/S/A/B/C separate on hue
  *and* saturation — SS and B are 120 vs 101 in hue alone, too close to call).
* **The panel's skip list is by fingerprint**, so an entry still names the same
  boss after the list reflows, and a boss that an event removes is *retired*
  rather than forgotten — it returns as itself, with your choice intact.
* **Party members are friends at or below your level**, strongest first. NPC
  recruits are token-priced and excluded three separate ways.

**Exam rune puzzle.** The same Mastermind the Kekkai TP mission uses, which the
exams also run — verified over code lengths 2 to 5 (their four table sizes),
with the length read from the seal's node count rather than configured. The
*navigation* to an exam is not implemented: nobody has captured those screens
yet. So navigate to the exam yourself and press Run; if there is no puzzle on
screen the bot says so and saves the frame, which is what the navigation would
be built from.

## The renderer changes the pixels

Ruffle can draw through `wgpu-webgl` (the default), `webgl` or `canvas`, and
the panel switches between them. **This is not cosmetic: it changes what the
bot sees.** Measured on the same screen minutes apart, with only the backend
changed:

| anchor | wgpu-webgl | webgl |
|---|---|---|
| `mission_room_entry` | 0.997 | 0.531 |
| `result_panel` | 1.000 | 0.341 |
| `mission_success` | 1.000 | 0.418 |

The split is not "legacy webgl is broken" — `webgl` and `canvas` agree with
each other to 0.001 and both disagree with wgpu. wgpu draws text **with its
stroke**; the others draw a thinner, unstroked face. Anything that is art is
pixel-identical; anything whose discriminating content is *lettering* is not.

So three things follow the backend, and each falls back per item rather than
wholesale:

* **`tpl/<renderer>/`** overrides a template crop by name. Eight take the farm
  from "cannot leave the village" on webgl to a mission banked end to end.
* **`ref/auto/tp/digits_ink/<renderer>/`** overrides a kekkai digit exemplar by
  digit — a backend needs only the digits that actually fail on it.
* **The cooling-skill gate** reads colour, so it is calibrated per backend. It
  is measured for wgpu and **not** for webgl, where an uncalibrated backend
  answers *unknown* rather than confidently wrong.

That last one is the practical difference today: on wgpu a cooling skill is
skipped outright, on webgl it is clicked and costs a ~6 s resolve timeout.
Nothing else prefers one backend over the other — both render at ~120 fps
against a 24 fps SWF.

`renderMode` is stored by the *site*, in `localStorage`, so it is per browser
profile and does not travel between machines. Before diagnosing a platform
difference, read the log line that names the backend.

## Performance and unattended running

Both measured, not estimated.

**Perception is ~7.5x faster than it was.** `matchTemplate` costs time linear in
frame area, and one template against a 3440x1440 capture measured **73 ms** — so
scoring all 60 cost 4.53 s and one resume-ladder step ~1.7 s. Two changes
compound, verified over 6018 template/frame comparisons with **zero** changes to
any decision or coordinate:

* search only the game rect (the rest of the capture is desktop wallpaper and
  the bot's own panel), which also *improves* discrimination because there is
  less unrelated art to match by accident;
* reject negatives at half resolution first — 96% of them, at a quarter of the
  cost — and pay full price only for candidates that could still clear their
  threshold.

**Starting a farm no longer stalls.** `in_mission` used to run the command-bar
geometry sweep before any template, so pressing Run in the village paid a 12.96 s
cold sweep to be told there was no command bar. Asking the cheap questions first
takes that to 0.43 s. The panel froze along with it, because nothing captures
during a sweep and the operator's buttons are only read on a capture.

**The mission list is not re-walked every cycle.** Paging to the level ceiling
cost 30 s of a 172 s cycle — the page *turn* is 0.19 s, but the per-page scan is
~3.7 s, seven times over. The answer is remembered per grade and verified before
it is trusted, forgotten on a level-up (which moves the ceiling *upwards*, where
verification cannot see it) and re-read every few missions in case one was
missed.

**It survives being left alone.** The machine is kept out of the idle state
while a run is in progress (the bot's own clicks cannot do this — CDP events
never reach the OS HID layer). Sleep cannot be prevented, but it is detected
exactly — the monotonic clock does not tick while suspended and the wall clock
does — and waking triggers a relog, because the game session will not have
survived it. A looping task recovers from a setback and carries on rather than
pausing, bounded so a deterministic fault still stops and says so.

## Layout

| Path | |
|---|---|
| `engine/app.py` | entry point: the bot window and its control loop |
| `engine/dock.py` | the injected control panel |
| `engine/cdp.py` | DevTools protocol client, standard library only |
| `engine/browser.py` | browser launch — the only OS-specific code |
| `engine/capture.py` | CDP frame → OpenCV, plus clipped reads |
| `engine/perceive.py` | template matching, template loading, colour masks, bar reads |
| `engine/geometry.py` | anchor-relative battle geometry |
| `engine/gate.py` | "wait until one of these is true" |
| `engine/battle.py` | turn loop, skill priority, restricted-turn handling |
| `engine/combat.py` | cooldown bookkeeping, damage watchdog |
| `engine/mission.py` | mission state machine, traversal, close-out |
| `engine/farm.py` | grade and mission selection |
| `engine/resume.py` | the ladder that gets back to the lobby from anywhere |
| `engine/kekkai*.py`, `cards.py`, `seals.py` | the three TP minigames |
| `engine/ss.py`, `balance.py`, `lights.py` | SS training: the multi-stage rune puzzle, Balance Control, Lights Out |
| `engine/eudemon.py`, `roster.py` | the Eudemon boss ladder and party recruiting |
| `engine/recut.py` | re-cuts a template for a renderer that draws it differently |
| `Start NS Bot.command`, `.bat`, `start-ns-bot.sh` | double-click launchers |
| `Configs/` | thresholds, geometry, rotation — no logic in code |
| `tpl/` | templates |
| `CLAUDE.md` | measured constants, corrections, and why each one is there |
| `engine/presence.py` | keeps the machine out of the idle state during a run |
| `engine/tasks.py` | what a task is: the registry the panel and the loop share |
| `tests/test_battle_stack.py` | 1,245 checks against recorded frames |

## Safety

Enforced in code, not by convention:

* **Never clicked, at any confidence:** character deletion, and the once-per-day
  actions (daily claim, wishing tree, lucky spin). Blocked in the policy and
  again at the click site.
* **Tokens — the premium currency — are never spent.** Losing a boss raises
  *"Do you want to revive by using 50 token?"* with a green check and a red X,
  and the ladder's generic "acknowledge a lone green check" rung matched that
  check at **0.979**. The distinction it was missing is structural: one green
  check is an *acknowledgement*, a green check **and** a red X is a *choice* —
  and pressing green accepts. A dialog offering both is now declined, never
  accepted, recognised by the flat panel between its two buttons (colour std
  2.8 against 61–65 on a battlefield). NPC party recruits are token-priced and
  excluded three ways.
* **The senjutsu orb is never pressed.** The magatama beside `S8` swaps the
  whole skill bar to the senjutsu set — it costs nothing and animates nothing,
  so `S1..S8` keep clicking while playing jutsu nobody chose. It is a no-click
  point, re-armed every turn from the command-bar anchor, and if the bar has
  been swapped anyway the runner puts it back.
* **`Share` is never clicked** on a reward panel. It publishes to a social
  feed. The close-out presses the panel's X, located by template and
  constrained to its corner.
* **Credentials are never handled.** The browser profile holds the session; on a
  logged-out or login screen the bot halts and says so.
* **The control panel is a no-click zone.** It is injected into the game's page,
  so its buttons are as clickable as anything else; bot clicks landing in it are
  refused and logged.
* **One runner at a time.** A pid lock stops a second instance attaching — eight
  were once found running together, each clicking the same game.
* **Padlocked missions are refused.** An above-level row is inert, so clicking it
  does nothing forever.
* **`Run` is not taken by default.** Fleeing fails the mission, which is a worse
  outcome than a slow fight. `combat.watchdog_action = "run"` restores it.
* Logs redact URLs and console output, both of which can carry a session token.

## Known gaps

* **The resume ladder cannot leave a battle.** It deliberately does not classify
  a fight or a traversal map, so a task needing the lobby cannot start from
  inside one. A relog covers it — character select is a screen the ladder
  knows — but a fight it cannot end still has no exit of its own.
* **The skill-cooldown refusal message has no template.** Cooling skills are
  detected from the icon instead, so on wgpu this costs nothing; the early-out
  is written and dormant, and activates by itself if the message is ever cut to
  `tpl/skill_cooldown.png`. On **webgl** it would earn its keep, because the
  icon check abstains there and each unusable skill costs a ~6 s timeout.
* **The kekkai digit exemplars are incomplete per backend.** They are harvested
  from play, and a digit with no exemplar is *refused*, never guessed — which
  stops a mission rather than corrupting the solver, but stops it all the same.
  wgpu currently covers 0–4; 5 and 6 fall back to the shared set. Each refusal
  saves the whole panel, and one saved panel yields several labelled exemplars
  because the log records the feedback for every row it *did* read.
* **wgpu is calibrated for cooling skills and webgl is not**, so the two
  backends are not equally good at the same things. Neither is strictly better.
* **`cv2` thread count cannot be capped on this build.** The macOS wheel uses
  GCD, where `setNumThreads` is a no-op, so the only way to reduce CPU burn is
  to do less work. The call is kept for Linux/Windows wheels, which honour it.
* **Traversal heading is a guess unless the game draws its "Go!" badge.** Where
  the badge is present it is authoritative — walk toward it — and it has been
  measured to overrule a wrong guess (character right of centre, so the spawn
  rule said left, which was a dead end seven runs running). Where it is absent
  the spawn rule applies and a wrong first guess costs one run. What a leftward
  map draws is still unknown, which is why the badge is read by position rather
  than by the direction of its glyph.
* `close_popup_x_large` never matches at any scale; the Daily Login Calendar is
  unclassifiable until it is re-cut.
* No positive lobby anchor beyond the icon rail — the village labels are
  semi-transparent over animated art and unusable.
* Pinned missions are positional, so a reordered list would farm the wrong one.
  Auto has no such exposure, which is why it is the default.

## One thing worth knowing

Almost every wrong turn in this project came from judging something by eye — a
bar, "nothing changed", whether two arrows looked alike. Each was contradicted by
a measurement:

* a "frozen" battle was a normal turn wait
* enemies "taking no damage" were on a loose mask reading every bar as 100%
* a kill read as regeneration, because the *lowest* enemy bar jumps up when the
  weakest one dies
* two page arrows that are indistinguishable at 3x magnification separate at
  1.000 vs 0.806 under `matchTemplate`
* a command-bar check that "felt slow" was 12.3 seconds of scale sweeping
* an SS puzzle that "froze" for 53 seconds was a `set()` rebuilt once per
  candidate — 48.92 s against 0.0019 s hoisted, and the reason the same code
  never stuttered on TP is simply that its pool is 216 rather than 46,656
* a digit reader that kept stalling was not misreading anything: its gate sat
  at 0.80 while correct reads measured 0.726–0.987 and wrong ones 0.396–0.627,
  so it was refusing a fifth of the answers it should have accepted

Thresholds and masks are calibrated against reference frames for that reason, and
`CLAUDE.md` records the specifics — including the corrections, which are the most
useful part of it.
