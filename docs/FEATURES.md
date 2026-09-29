# What the bot can do, and where it stops

Companion to the [README](../README.md), which covers installing and running
it. Everything here was measured against the live game; `CLAUDE.md` holds the
raw numbers and the corrections behind them.

---

## What it plays

### Story missions

Reads the grade panel by colour (A hue ~103, B ~51, C ~128; a locked grade
renders grey and never reaches saturation), pages forward to the first padlock
and takes the last unlocked row before it, then plays the mission: dialogue,
traversal, combat and the Mission Success close-out. A grade that runs out of
pages before showing a padlock stops on its last page instead.

Enemies are found by **movement** rather than colour, which cannot be fooled by
scenery — with the exception of *animated* scenery, so a mover that stays
unreachable is eventually treated as scenery too.

Cooldown lengths are **learned by bracketing them from outcomes**, since the
game does not expose them: used at round U and fired again at R means the
cooldown is at most R−U; used at U and refused at R means it is more than R−U.
Squeeze until they meet.

Measured: **18 missions banked back to back, ~172 s per cycle**, no
intervention.

### TP training

Five missions a day. The minigame is identified **from the screen, not from the
mission name** — the pixels win, which is what lets one pass cover three
different games without being told which is which.

| minigame | state |
|---|---|
| Kekkai (rune Mastermind) | solved |
| Scroll (memory board) | solved — cleared 20/20 with 51 s to spare |
| Potion (hand-seal memorisation) | solved — including eight-sign rounds |

The pass has no mission cap: it keeps taking startable rows until the list
offers none. A completed row greys out and stops being detected, so only
*failures* are remembered — by a fingerprint of what the row says, because
survivors reflow upward into the slot a finished mission vacated and a
remembered *position* would then name a different mission.

Measured: **5 of 5 banked**, the day's list finishing empty.

### SS training

Five missions a day at five times the TP reward, in three families, again read
off the screen.

| minigame | state |
|---|---|
| Sage Power Seal (rune Mastermind, multi-stage) | solved — stages escalate 5 → 6 runes |
| Balance Control (subset sum) | solved |
| Sage Sealed Boxes (Lights Out) | solved — 3x3 over GF(2), verified against all 512 states |

Three things make these harder than their TP cousins:

* **Ten guesses per stage, and running out fails the *mission*.** So the solver
  never spends a guess probing, and it resumes from the rows already on the
  scroll rather than restarting.
* **The code length is re-read per stage** from the seal's node count, never
  carried over — a five-node stage is followed by a six-node one.
* **Balance Control hides its sums** on later stages (`??`), so the target is
  *derived* rather than read: swapping only moves values between columns, so
  the total is invariant and the target is half of it.

Measured: **5 of 5 banked** — two combats, a rune, a balance and a lights, in
one pass.

### Eudemon Garden

A boss ladder reached through the Hunting House. Each lap recruits a party,
enters the garden, fights, and ends back in the village — because teammates
leave after every boss, so a party filled once is gone by the second fight.

* **Bosses are read off the list**, not configured: 14 on this account across
  three pages, rank taken from the badge colour. SS and B are 120 vs 101 in hue
  alone, too close to call, so saturation decides as well.
* **The skip list is keyed by fingerprint**, so an entry still names the same
  boss after the list reflows. A boss an event removes is *retired* rather than
  forgotten, and returns as itself with your choice intact.
* **Party members are friends at or below your level**, strongest first.
  Token-priced NPC recruits are excluded three separate ways.

Measured: **8 bosses banked in a row**, recruiting a party each lap.

### Exam rune puzzle

The same Mastermind the Kekkai TP mission uses, which the exams also run —
verified over code lengths 2 to 5, with the length read from the seal's node
count rather than configured.

**The navigation to an exam is not implemented**: nobody has captured those
screens. Navigate to the exam yourself and press Run; if there is no puzzle on
screen the bot says so and saves the frame, which is what the navigation would
be built from.

---

## The renderer changes the pixels

Ruffle can draw through `wgpu-webgl` (the default), `webgl` or `canvas`, and
the panel switches between them. **This is not cosmetic: it changes what the
bot sees.** Measured on the same screen minutes apart, only the backend changed:

| anchor | wgpu-webgl | webgl |
|---|---|---|
| `mission_room_entry` | 0.997 | 0.531 |
| `result_panel` | 1.000 | 0.341 |
| `mission_success` | 1.000 | 0.418 |

The split is not "legacy webgl is broken" — `webgl` and `canvas` agree with
each other to 0.001 and both disagree with wgpu. wgpu draws text **with its
stroke**; the others draw a thinner, unstroked face. Anything that is art is
pixel-identical; anything whose discriminating content is *lettering* is not.

So three things follow the backend, each falling back per item rather than
wholesale:

* **`tpl/<renderer>/`** overrides a template crop by name. Eight take the farm
  from "cannot leave the village" on webgl to a mission banked end to end.
* **`ref/auto/tp/digits_ink/<renderer>/`** overrides a kekkai digit exemplar by
  digit — a backend needs only the digits that actually fail on it.
* **The cooling-skill gate** reads colour, so it is calibrated per backend. It
  is measured for wgpu and **not** for webgl, where it answers *unknown* rather
  than confidently wrong.

**Neither backend is strictly better.** On wgpu a cooling skill is skipped
outright; on webgl it is clicked and costs a ~6 s resolve timeout. Against
that, the shared digit exemplar set is the better-covered one. Both render at
~120 fps against a 24 fps SWF, so speed is not the deciding factor.

`renderMode` is stored by the *site*, in `localStorage`, so it is per browser
profile and does not travel between machines. Before diagnosing a platform
difference, read the log line naming the backend.

---

## Performance

All measured, not estimated.

**Perception is ~7.5x faster than it was.** `matchTemplate` costs time linear
in frame area, and one template against a 3440x1440 capture measured **73 ms** —
so scoring all 60 cost 4.53 s. Two changes compound, verified over 6,018
template/frame comparisons with **zero** changes to any decision or coordinate:

* search only the game rect (the rest is desktop wallpaper and the bot's own
  panel), which also *improves* discrimination because there is less unrelated
  art to match by accident;
* reject negatives at half resolution first — 96% of them, at a quarter of the
  cost — and pay full price only for candidates that could still clear their
  threshold.

**Starting a farm no longer stalls.** `in_mission` used to run the command-bar
geometry sweep before any template, so pressing Run in the village paid a
12.96 s cold sweep to be told there was no command bar. Asking the cheap
questions first takes that to 0.43 s.

**The mission list is not re-walked every cycle.** Paging to the level ceiling
cost 30 s of a 172 s cycle. The answer is remembered per grade, verified before
it is trusted, forgotten on a level-up, and re-read every few missions.

**The SS rune solver no longer freezes.** Intersecting two candidate pools was
written `[c for c in pa if c in set(pb)]`, rebuilding the set once per element:
**48.92 s** at length 6 against **0.0019 s** hoisted. The same line costs 0.00 s
on TP, whose pool is 216 rather than 46,656 — which is why only SS ever
stuttered.

---

## Unattended running

The machine is kept out of the idle state while a run is in progress — the
bot's own clicks cannot do this, because CDP events never reach the OS HID
layer.

Sleep cannot be prevented, but it is **detected exactly**: the monotonic clock
does not tick while suspended and the wall clock does, so the difference is the
time asleep. Waking triggers a relog, because the game session will not have
survived it.

A looping task recovers from a setback and carries on rather than pausing,
bounded so a deterministic fault still stops and says so.

---

## Safety in detail

The rules are listed in the README; this is why each exists.

**Tokens are never spent.** Losing a boss raises *"Do you want to revive by
using 50 token?"* with a green check and a red X — and the resume ladder's
generic "acknowledge a lone green check" rung matched that check at **0.979**.
The distinction it was missing is structural:

    one green check              an ACKNOWLEDGEMENT  -> pressing it is safe
    a green check AND a red X    a CHOICE            -> pressing green ACCEPTS

A dialog offering both is declined, never accepted. It is recognised by the
**flat panel between its two buttons** — colour std 2.8 against 61–65 on a
battlefield — because two coloured discs alone also describes a skill icon
beside a turn marker, which is exactly what an earlier version pressed.

**The senjutsu orb is never pressed.** The magatama beside `S8` swaps the whole
skill bar to the senjutsu set. It costs nothing and animates nothing, so
`S1..S8` keep clicking while playing jutsu nobody chose. It is a no-click
point, re-armed every turn from the command-bar anchor; if the bar has been
swapped anyway the runner puts it back, and only on a positive reading.

**`Share` is never clicked** on a reward panel — it publishes to a social feed.
The close-out presses the panel's X, located by template and constrained to its
corner.

**`Play` is clicked by template, never by offset**, because `Delete` sits
beside it on character select.

**The pid lock verifies identity**, not merely that something holds the pid.
Eight instances were once found running together, each clicking the same game.

---

## Known gaps

* **The resume ladder cannot leave a battle.** It deliberately does not
  classify a fight or a traversal map, so a task needing the lobby cannot start
  from inside one. A relog covers it — character select is a screen the ladder
  knows — but a fight it cannot end still has no exit of its own.
* **Kekkai digit exemplars are incomplete per backend.** They are harvested
  from play, and a digit with no exemplar is *refused*, never guessed — which
  stops a mission rather than corrupting the solver, but stops it all the same.
  wgpu currently covers 0–4; 5 and 6 fall back to the shared set. Each refusal
  saves the whole panel, and one panel yields several labelled exemplars,
  because the log records the feedback for every row it *did* read.
* **The skill-cooldown refusal message has no template.** On wgpu the icon
  check covers it; on **webgl** a template would earn its keep, because the
  icon check abstains there and each unusable skill costs a ~6 s timeout.
* **Traversal heading is a guess unless the game draws its "Go!" badge.** Where
  present it is authoritative and has overruled a wrong guess. Where absent the
  spawn rule applies and a wrong first guess costs one run. What a leftward map
  draws is still unknown, which is why the badge is read by position rather
  than by the direction of its glyph.
* **`cv2` thread count cannot be capped on the macOS build.** That wheel uses
  GCD, where `setNumThreads` is a no-op, so the only way to reduce CPU burn is
  to do less work. The call is kept for Linux/Windows wheels, which honour it.
* `close_popup_x_large` never matches at any scale; the Daily Login Calendar is
  unclassifiable until it is re-cut.
* No positive lobby anchor beyond the icon rail — the village labels are
  semi-transparent over animated art and unusable.
* Pinned missions are positional, so a reordered list would farm the wrong one.
  Auto has no such exposure, which is why it is the default.

---

## One thing worth knowing

Almost every wrong turn in this project came from judging something by eye — a
bar, "nothing changed", whether two arrows looked alike. Each was contradicted
by a measurement:

* a "frozen" battle was a normal turn wait
* enemies "taking no damage" were on a loose mask reading every bar as 100%
* a kill read as regeneration, because the *lowest* enemy bar jumps up when the
  weakest one dies
* two page arrows indistinguishable at 3x magnification separate at 1.000 vs
  0.806 under `matchTemplate`
* a command-bar check that "felt slow" was 12.3 seconds of scale sweeping
* an SS puzzle that "froze" for 53 seconds was a `set()` rebuilt once per
  candidate
* a digit reader that kept stalling was not misreading anything — its gate sat
  at 0.80 while correct reads measured 0.726–0.987 and wrong ones 0.396–0.627,
  so it refused a fifth of the answers it should have accepted

Thresholds and masks are calibrated against reference frames for that reason,
and `CLAUDE.md` records the specifics — including the corrections, which are
the most useful part of it.
