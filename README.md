# NS Bot

UI-level automation for Ninja Saga, a Flash game running on Ruffle. The bot looks
at rendered pixels and clicks. It does not touch game memory, network traffic or
the server protocol.

**Status: farms story missions and TP training unattended.** Grade selection,
mission choice, dialogue, traversal, combat and close-out all run without help.
Two of the three TP minigames are solved end to end; the third is playable.

---

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python engine/app.py            # launches the browser
.venv/bin/python engine/app.py --attach   # or attaches to one already open
```

That opens a window with the game on the left and a control panel on the right.
Sign in once — the browser profile keeps the session.

**Prefer `--attach` whenever the browser is already up.** The site's session
cookie is a browser-session cookie, so quitting Chrome signs you out.

Only one dependency: `opencv-python-headless`. The DevTools client, browser
launcher and control plane are standard library.

## The bot window

Chrome is launched with `--app=`, which drops the tab strip, omnibox and
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
| **Task** | resume to lobby, TP training, farm missions, idle |
| **Run** | run / pause / relog / stop, and quit |
| **Farm target** | grade (auto, S, A, B, C) and mission (highest, or a pinned page/row) |
| **Skill order** | click `AT CH DO S1..S8` to build a priority order; Attack is the floor |
| **Focus mode** | hides the page chrome and pins the game to the top |

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
dialogue, traversal, combat, and the Mission Success close-out.

**TP training.** Missions are chosen by position and the minigame is identified
from the screen, not from the mission name.

| minigame | state |
|---|---|
| Kekkai (rune Mastermind) | solved — missions banked |
| Scroll (memory board) | solved — cleared 20/20 with 51s to spare |
| Potion (hand-seal memorisation) | solved — five levels including an eight-sign round |

## Layout

| Path | |
|---|---|
| `engine/app.py` | entry point: the bot window and its control loop |
| `engine/dock.py` | the injected control panel |
| `engine/cdp.py` | DevTools protocol client, standard library only |
| `engine/browser.py` | browser launch — the only OS-specific code |
| `engine/capture.py` | CDP frame → OpenCV, plus clipped reads |
| `engine/tasks.py` | what a task is: the registry the panel and the loop share |
| `engine/perceive.py` | template matching, template loading, colour masks, bar reads |
| `engine/geometry.py` | anchor-relative battle geometry |
| `engine/gate.py` | "wait until one of these is true" |
| `engine/battle.py` | turn loop, skill priority, restricted-turn handling |
| `engine/combat.py` | cooldown bookkeeping, damage watchdog |
| `engine/mission.py` | mission state machine, traversal, close-out |
| `engine/farm.py` | grade and mission selection |
| `engine/resume.py` | the ladder that gets back to the lobby from anywhere |
| `engine/kekkai*.py`, `cards.py`, `seals.py` | the three TP minigames |
| `Configs/` | thresholds, geometry, rotation — no logic in code |
| `tpl/` | templates |
| `CLAUDE.md` | measured constants, corrections, and why each one is there |
| `tests/test_battle_stack.py` | 218 checks against recorded frames |

## Safety

Enforced in code, not by convention:

* **Never clicked, at any confidence:** character deletion, and the once-per-day
  actions (daily claim, wishing tree, lucky spin). Blocked in the policy and
  again at the click site.
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

* **The resume ladder cannot leave a battle.** Nothing wedges today, but a fight
  it cannot end has no exit.
* **Traversal heading is a coin flip on the first run of a map.** The Kekkai
  runner derives it from where the character spawned, but that finder keys on a
  red robe and this character wears purple, so it returns nothing and mission
  traversal alternates instead. A wrong first guess costs one run.
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

Thresholds and masks are calibrated against reference frames for that reason, and
`CLAUDE.md` records the specifics — including the corrections, which are the most
useful part of it.
