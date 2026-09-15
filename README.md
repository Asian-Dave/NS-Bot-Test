# NS Bot

UI-level automation for Ninja Saga, a Flash game running on Ruffle. The bot
looks at rendered pixels and clicks. It does not touch game memory, network
traffic or the server protocol.

It farms story missions, TP training, SS training and the Eudemon Garden boss
ladder unattended — see **[docs/FEATURES.md](docs/FEATURES.md)** for what each
of those means, what has been measured, and where the edges are.

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
double-click that goes wrong leaves something to read. They append to
`run/app.log` rather than overwriting it, so double-clicking while an instance
is already running cannot destroy the log of the one doing the work.

Or from a shell:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python engine/app.py            # launches, or reuses a live browser
.venv/bin/python engine/app.py --attach   # refuse to launch; attach or fail fast
```

Neither form needs to be told which case it is in: `browser.launch(reuse=True)`
attaches to a browser already serving CDP and starts one only when nothing is.

That opens a window with the game on the left and a control panel on the right.
**Sign in once** — the browser profile keeps the session.

**Prefer `--attach` whenever the browser is already up.** The site's session
cookie is a browser-session cookie, so quitting the browser signs you out and
only a human can sign back in.

Only one dependency: `opencv-python-headless`. The DevTools client, browser
launcher and control plane are standard library.

## Which browser

**Any Chromium-based browser works** — Chrome, Chromium, Edge, Brave, Vivaldi,
Opera, Arc. The requirement was never Chrome; it is CDP, which is Chromium's own
protocol. Verified live against Microsoft Edge. The first one installed is found
automatically; `--browser /path/to/binary` (or `target.browser` in the config)
overrides it, and the log says which was used.

**Firefox and Safari do not work**, and no path setting can fix that — they
speak WebDriver BiDi and the WebKit Inspector Protocol respectively, so either
would mean a second transport for capture, input and script injection.

## The panel

The browser is launched with `--app=`, so there is no tab strip or omnibox, and
the panel is injected into the game's own page in the wallpaper gutter beside
the canvas. The game renders natively — there is no streaming in the viewing
path.

| | |
|---|---|
| **Task** | resume to lobby, TP training, SS training, Eudemon hunt, farm missions, exam rune puzzle, idle |
| **Run** | run / pause / relog / stop, and quit |
| **Stop** | aborts the task, clears its progress, and relaunches attached — no terminal trip |
| **Quit** | removes the panel and exits for good |
| **Farm target** | grade (auto, S, A, B, C) and mission (highest, or a pinned page/row) |
| **Skill order** | click `AT CH DO S1..S8` to build a priority order; Attack is the floor |
| **Hunt skill order** | a second rotation for Eudemon/Hunting House fights; empty falls back to the main order |
| **Eudemon bosses** | every boss the scan has seen, by name — click one to skip it |
| **Scan bosses now** | pages the whole garden and refreshes that list |
| **View** | focus mode, window size, renderer and server |

Settings take effect on the **next** mission and persist to `run/`, outside the
tracked config. If no bot is attached the panel says so rather than sitting
there with dead buttons.

**Focus mode is not decoration.** The page scroll drifts, and the game is 839
CSS px tall in a 720 px viewport, so which 119 px are hidden depends on where
the page happens to be scrolled — which has caused "could not find the Special
tab" on a healthy Mission Room. Hiding the game's siblings lets it reflow to
the top so `scrollY` is 0 and stays 0.

## How it works

```
CDP capture → template match → state classify → policy → click (CDP)
```

* **Capture** is `Page.captureScreenshot`, not screen grabbing. No
  screen-recording permission, and the window need not be frontmost.
* **Clicks** are `Input.dispatchMouseEvent`, which reach the Ruffle canvas as
  trusted events.
* **The game is a single canvas.** There are no DOM elements inside it, so
  template matching is the only interface available.
* **Waiting is always a gate, never a sleep.** Clicks issued during the enemy's
  turn are silently discarded by the game, which is why sleeping is the wrong
  tool.

## Safety

Enforced in code, not by convention. The reasoning behind each is in
[docs/FEATURES.md](docs/FEATURES.md#safety-in-detail).

* **Tokens — the premium currency — are never spent.** A dialog offering a
  green check *and* a red X is a choice, not an acknowledgement, and is
  declined. Token-priced NPC party recruits are excluded.
* **Credentials are never handled.** The browser profile holds the session; on
  a logged-out or login screen the bot halts and says so.
* **Never clicked, at any confidence:** character deletion, the once-per-day
  actions (daily claim, wishing tree, lucky spin), `Share` on a reward panel,
  and the senjutsu orb.
* **The control panel is a no-click zone**, since it is injected into the
  game's page and its buttons are as clickable as anything else.
* **One runner at a time**, enforced by a pid lock that verifies identity and
  not merely that *something* holds the pid.
* **`Run` is not taken by default** — fleeing fails the mission.
* Logs redact URLs and console output, both of which can carry a session token.

## Layout

| Path | |
|---|---|
| `engine/app.py` | entry point: the bot window and its control loop |
| `engine/dock.py` | the injected control panel |
| `engine/cdp.py` | DevTools protocol client, standard library only |
| `engine/browser.py` | browser launch — the only OS-specific code |
| `engine/capture.py` | CDP frame → OpenCV, plus clipped reads |
| `engine/perceive.py` | template matching and loading, colour masks, bar reads |
| `engine/geometry.py` | anchor-relative battle geometry |
| `engine/gate.py` | "wait until one of these is true" |
| `engine/battle.py` | turn loop, skill priority, restricted-turn handling |
| `engine/combat.py` | cooldown bookkeeping, damage watchdog |
| `engine/mission.py` | mission state machine, traversal, close-out |
| `engine/farm.py` | grade and mission selection |
| `engine/resume.py` | the ladder that gets back to the lobby from anywhere |
| `engine/tasks.py` | what a task is: the registry the panel and the loop share |
| `engine/kekkai*.py`, `cards.py`, `seals.py` | the three TP minigames |
| `engine/ss.py`, `balance.py`, `lights.py` | the three SS minigames |
| `engine/eudemon.py`, `roster.py` | the Eudemon boss ladder and party recruiting |
| `engine/presence.py` | keeps the machine out of the idle state during a run |
| `Configs/` | thresholds, geometry, rotation — no logic in code |
| `tpl/` | templates, with `tpl/<renderer>/` overrides |
| `tests/test_battle_stack.py` | 1,245 checks against recorded frames |
| `docs/FEATURES.md` | what it plays, what is measured, where the edges are |
| `CLAUDE.md` | measured constants, corrections, and why each one is there |
