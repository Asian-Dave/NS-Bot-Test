# NS Bot

UI-level automation for Ninja Saga (`ninjasaga.cc`), a Flash game running on
Ruffle. The bot looks at rendered pixels and clicks — it does not touch game
memory, network traffic or the server protocol.

**Status: perception and control work end to end. One behaviour is armed.**
The bot can enter the game unattended (select character → Play) and reports state,
template confidences, enemy health and turn state every cycle. Everything else
detects and logs but does not act.

---

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python engine/dashboard.py --embed
```

Open **http://127.0.0.1:8770/embed** in the Chrome window it launches. Sign in
once inside the embedded frame — the profile keeps the session afterwards.

Only one dependency: `opencv-python-headless`. The DevTools client, browser
launcher, dashboard and control plane are all standard library.

## How it works

```
CDP capture → template match → state classify → policy → click (CDP)
```

* **Capture** is `Page.captureScreenshot`, not screen grabbing. No
  screen-recording permission, and the window need not be frontmost.
* **Clicks** are `Input.dispatchMouseEvent`, which arrive at the Ruffle canvas as
  trusted events. `pydirectinput` is not used and cannot be — it is Windows-only.
* **The game is a single canvas.** There are no DOM elements inside it, so
  template matching is the only interface available.
* **The dashboard embeds the game in an iframe.** The page's own JavaScript
  cannot read a cross-origin frame, but it never needs to — the bot drives it over
  CDP, which is not bound by same-origin policy. Native rendering, no streaming.
  This only works when the page is open in the browser the bot controls.

## Layout

| Path | |
|---|---|
| `engine/cdp.py` | DevTools protocol client, standard library only |
| `engine/browser.py` | browser launch — the only OS-specific code |
| `engine/capture.py` | CDP frame → OpenCV |
| `engine/perceive.py` | template matching, colour masks, bar reads |
| `engine/combat.py` | turn gating, round cooldowns, stall watchdog |
| `engine/bot.py` | state classification and decision policy |
| `engine/dashboard.py` | localhost control panel |
| `configs/` | thresholds, skill slots, geometry — no logic in code |
| `tpl/` | templates (`_`-prefixed = quarantined, reason in `CLAUDE.md`) |
| `CLAUDE.md` | measured constants, discrimination matrix, corrections |

## Safety

Enforced in code, not by convention:

* **Never clicked, at any confidence:** character deletion, and the once-per-day
  actions (daily claim, wishing tree, lucky spin). Blocked in the policy and
  again at the click site.
* **Credentials are never handled.** The browser profile holds the session; on
  reaching a logged-out or login screen the bot halts and notifies.
* **Live clicking is opt-in per state.** Unarmed states detect and log only.
* **Stalled fights abort.** Enemies regenerate, so a weak attack loop can be
  cancelled out entirely, giving a fight with no error and no end.
* Logs redact URLs and console output, both of which can carry a session token.

## Known gaps

* `close_popup_x_large` never matches at any scale — the Daily Login Calendar is
  unclassifiable until it is re-cut.
* No positive lobby anchor; the lobby is currently identified negatively. The
  village labels are semi-transparent over animated art and unusable.
* `claim_daily` peaks at 0.79 against ~0.97 for its peers, probably an animated
  highlight. Needs re-measuring against a live capture.
* The embedded frame loads `/play`, so the game sits inside the full page. The
  panel's "Focus game" button switches to the game document directly.
* Combat detection is scale-sensitive; geometry must stay pinned.

## One thing worth knowing

Almost every wrong turn in this project came from estimating a bar, or "nothing
changed", by eye. Every one of those was contradicted by a measurement — and two
bugs in the stall watchdog only surfaced when it was replayed against real
recorded numbers. Thresholds and masks are calibrated against reference frames
for that reason, and `CLAUDE.md` records the specifics.
