"""Resume dispatcher — "where am I, and what is the one step that advances?"

WHY THE BOT APPEARED TO FREEZE
------------------------------
It wasn't hung. It was cycling happily at `state=character_select`, emitting
`click character_card` every pass, and never getting anywhere. Two causes, both
structural:

1. `bot.identify_state` + `decide_action` is a SINGLE-SHOT classifier. It maps one
   frame to one action. There is no chain that carries the session from character
   select through Play, through the four queued login popups, into the lobby, and
   on to a task. Each cycle re-decided the same first step.
2. It was in dry-run, so the step it decided was never taken.

The reference bot feels seamless because it has the thing we were missing. In
`FormMain.cs` around 5327-5380 there is a long if / else-if chain over pixel
anchors, re-entered on EVERY cycle:

    if      Loading pixel        -> wait for it to clear
    if      notice/popup pixel   -> click it
    else if Login pixel          -> login handler
    else if Username pixel       -> character/username handler
    else if (690,405,#008E00)    -> confirm handler
    else if Village pixel        -> we have arrived; run the actual task
    else if <task-specific>      -> ...

The properties that make that seamless are worth naming, because they are the
design and not incidental:

  * **Re-entrant.** It never assumes where it is. Every cycle re-identifies the
    screen from scratch, so it can be started mid-login, mid-loading, in the
    lobby, or in a battle, and it converges to the goal from any of them.
  * **One step per pass, then return.** Each branch does a single action and
    hands back to the dispatcher, which re-identifies. No branch tries to drive a
    multi-screen sequence itself.
  * **Idempotent.** Doing the same step twice is harmless, because the next pass
    just re-reads the screen. That is what makes it robust to a click that missed.
  * **A single "we have arrived" anchor.** `Ui["Village"]` = one pixel,
    `(46,90,#003A8F)`. Reaching it means stop navigating and start working.
  * **Loading is handled first, and by waiting** — never by clicking.

This module is that chain, on our stack.

WHAT WE DO DIFFERENTLY
----------------------
* Their anchors are single exact pixels; ours are templates, because our canvas
  geometry is not guaranteed fixed. Where a template is expensive and a pixel
  would do, `gate.pixel` is available — see PERFORMANCE below.
* **We never authenticate.** Their chain includes a login handler that types
  stored credentials (`Global` carries Username/Password). We do not have that
  branch and will not: sessions live in the browser profile, which is the
  credential store. A login screen is a HALT, and a human deals with it. That is
  a deliberate divergence from the reference, not an omission.

PERFORMANCE — measured, and the reason this is usable
-----------------------------------------------------
The old loop scored all 32 templates across a 36-step scale sweep: **52 seconds**
per cycle on a live 1920x1678 frame. At native scale only: **1.5 s**. And every
template that actually hit peaked at exactly **1.0**.

That is not a coincidence. We pin the viewport with
`Emulation.setDeviceMetricsOverride`, so exactly one geometry ever occurs and the
templates were cut at it. Searching scale at runtime is searching for something
that cannot vary. CLAUDE.md already says: pin the viewport so only one geometry
occurs, OR re-cut at one canonical geometry. Having pinned it, we get to stop
sweeping.

So this dispatcher does two things the old loop did not:
  * matches at native scale only (`Template.scales` defaults to [1.0])
  * **short-circuits** — checks anchors in priority order and stops at the first
    hit, instead of scoring all 32 every pass

Keep the sweep for calibration (`engine/calibrate.py`) and for the diagnostic
loop in `bot.py`, where measuring the true peak is the entire point. Never in the
hot path.
"""
import time

from perceive import find


# --- outcomes ---------------------------------------------------------------
ARRIVED = "arrived"        # we are in the lobby; the caller may start its task
HALT = "halt"              # a human must intervene (login, or an unknown wall)
STOPPED = "stopped"        # operator asked us to stop
WORKING = "working"        # took a step; call again


class Step:
    """One rung of the ladder.

    `anchor`   template name that identifies the screen
    `action`   'click' | 'wait' | 'arrive' | 'halt'
    `target`   template name to click (defaults to `anchor`)
    `offset`   optional (dx, dy) applied to the target's centre
    """

    def __init__(self, name, anchor, action, target=None, offset=(0, 0),
                 threshold=None, note="", target_scales=None,
                 target_threshold=None):
        self.name = name
        self.anchor = anchor
        self.action = action
        self.target = target or anchor
        self.offset = offset
        self.threshold = threshold
        self.note = note
        # A target template may need its own scale sweep and gate, independent of
        # the anchor's. The green check needs exactly that: it is ONE glyph drawn
        # at three sizes (detail panel 1.00, Victory 1.18, Mission Success 1.84),
        # so a single-scale lookup finds it on one panel and misses it on another.
        self.target_scales = target_scales
        self.target_threshold = target_threshold


# The ladder, in PRIORITY ORDER. First match wins and the pass ends.
#
# Ordering rules, each from a measurement in CLAUDE.md:
#   * loading first — it hides everything else, and must be waited out not clicked
#   * popups before lobby — the login queue is FOUR popups deep, and the lobby
#     rail still reads ~0.69 behind a popup, so a lobby-first order would declare
#     arrival while a modal is still up
#   * character_select before lobby for the same reason
#   * the lobby anchor last: it is the goal, not a waypoint
DEFAULT_LADDER = [
    # LOGGED OUT: halt, loudly and immediately. Never authenticate - CLAUDE.md
    # is explicit that an expired session may mean a password change or a ban,
    # and that it must reach the human rather than be auto-recovered. Without
    # this rung the ladder still refused to log in, but only by exhausting
    # `max_unknown` and reporting "unrecognised screen", which tells the
    # operator nothing about what is actually wrong.
    #
    # Anchor is the logged-out page's own "Welcome, Shinobi!" heading:
    # 1.000 positive against a 0.435 worst negative.
    Step("logged_out", "logged_out", "halt",
         note="you are signed out - a human must sign in; the bot never "
              "handles credentials"),
    Step("loading", "loading_text", "wait",
         note="cold SWF load 25-30s, warm ~8s; Hunting House hangs at 3% forever"),

    # --- result panels. These must be cleared BEFORE the popup rungs and long
    # before the lobby anchor: the persistent lobby rail still reads behind a
    # panel, so a lobby-first order would declare arrival with a panel open.
    #
    # Both are dismissed by their GREEN CHECK, never by clicking the panel body
    # or the banner. MEASURED: a Victory panel absorbed ELEVEN centre clicks and
    # did nothing. And the check is one glyph at three sizes (detail 1.00,
    # Victory 1.18, Mission Success 1.84), hence the wide target sweep - a narrow
    # one scored 0.693 on Mission Success and the mission could never close out.
    #
    # Why these belong in the LADDER and not only in MissionRunner: a mission can
    # end while only `resume_to_lobby` is armed, and the ladder then sat on the
    # panel for 41 consecutive cycles doing nothing. The ladder's job is to get
    # back to a known state from anywhere, and "a result panel is open" is one of
    # those anywheres.
    Step("mission_success", "mission_success", "click", target="mission_start",
         target_scales=[round(0.95 + i * 0.05, 2) for i in range(21)],
         target_threshold=0.85,
         note="end-of-mission panel; acknowledging it is what banks the reward "
              "and returns to the lobby"),
    Step("result_panel", "result_panel", "click", target="mission_start",
         target_scales=[round(0.95 + i * 0.05, 2) for i in range(21)],
         target_threshold=0.85,
         note="mid-mission Victory; XP 0 / Gold 0 here is NORMAL"),

    # A cutscene is a dead end for the ladder without this. Measured live: a
    # failed TP mission ends on "Aww... you better take some rest..." over a
    # "click anywhere to continue" screen, and the ladder halted there after 20
    # unrecognised frames - correctly refusing to click blindly, but unable to
    # get home from a screen whose only exit is a click.
    #
    # CLAUDE.md warns that `click_to_continue` was unusable as a gate (0.642 to
    # 0.849 across unrelated states, false-firing on combat). That was a
    # DIFFERENT, badly-cut template. `cutscene_continue` was re-cut and measured
    # across eleven reference frames: 0.968 positive against a 0.421 worst
    # negative (Mission Success), a margin of 0.547 - so it is safe here where
    # the old one was not.
    #
    # It sits AFTER the result panels deliberately: a Victory or Success panel
    # must be acknowledged by its own green check, not clicked through as if it
    # were a cutscene.
    Step("cutscene", "cutscene_continue", "click", threshold=0.80,
         note="click-anywhere screen; the only way off it is a click"),

    # --- popup drain. Login queues four: Daily Login Reward -> Calendar ->
    # Wishing Tree -> Lucky Spin. Dismiss controls are NOT uniform, hence four
    # separate anchors rather than one.
    Step("popup_x", "close_popup_x", "click"),
    Step("popup_x_menu", "close_popup_x_menu", "click"),
    Step("popup_x_large", "close_popup_x_large", "click", threshold=0.76,
         note="bad crop; flat 0.547 at every scale in the old matrix. Re-cut."),
    Step("popup_back", "close_popup_back_arrow", "click", threshold=0.80),
    # A FIFTH popup variant: the recurring shop promo ("HP/CP Scrolls are your
    # best friends") with a "Go to Shop" button. None of the four dismiss
    # templates above match it - measured best 0.678 - so the ladder used to stall
    # on it in the village, reporting `unknown` while the Mission Room entrance
    # sat hidden behind it. Its own X templates at 1.000 / 0.464 worst negative.
    # NOTE the button next to it navigates to the Shop; only the X is safe.
    Step("popup_promo", "close_promo_x", "click",
         note="recurring shop promo; dismiss by X, never 'Go to Shop'"),

    # --- character select. TWO steps, and the order is enforced by presence:
    # play_btn only exists AFTER a slot is selected.
    #
    # SAFETY: `delete_btn` sits on the same row as Play and permanently destroys
    # a character. Play is whitelisted BY TEMPLATE and never clicked by offset
    # from anything else. The card click uses an offset from char_slot_level, so
    # it is deliberately ordered AFTER play_btn — if Play is on screen we take it
    # and never compute an offset at all.
    Step("play", "play_btn", "click",
         note="whitelisted by template; never by offset (Delete is adjacent)"),
    Step("select_char", "char_slot_level", "click", offset=(-40, -20),
         note="the Level label sits inside the card; click the card"),

    Step("lobby", "lobby_rail_fortune", "arrive",
         note="positive lobby anchor: opaque icon rail, 0.997 here vs 0.356 in combat"),
]


class Resumer:
    """Walk the ladder until we arrive, halt, or are stopped.

    Deliberately holds no memory of where it thought it was. That is the whole
    point: `advance()` is safe to call from any screen at any time.
    """

    def __init__(self, capture, actor, templates, log, controls=None,
                 ladder=None, never_click=None):
        self.capture, self.actor = capture, actor
        self.templates, self.log = templates, log
        self.controls = controls
        self.ladder = ladder or DEFAULT_LADDER
        self.never_click = set(never_click or
                               ("delete_btn", "claim_daily", "wish_btn", "spin_btn"))
        self.unknown_streak = 0

    # -- one pass ------------------------------------------------------------
    def advance(self, frame_gray=None):
        """Identify the screen and take at most ONE step. Returns an outcome."""
        if self.controls is not None and not self.controls.wait_if_paused():
            return STOPPED, {"reason": "operator stop"}

        if frame_gray is None:
            import cv2
            frame_gray = cv2.cvtColor(self.capture.frame(gray=False),
                                      cv2.COLOR_BGR2GRAY)

        t0 = time.time()
        for step in self.ladder:
            tpl = self.templates.get(step.anchor)
            if tpl is None:
                continue
            m, conf = find(frame_gray, tpl)
            limit = step.threshold if step.threshold is not None else tpl.threshold
            if conf < limit:
                continue

            # matched — short-circuit here
            el = (time.time() - t0) * 1000
            self.unknown_streak = 0
            self.log.info("resume: %s (%s conf=%.3f) in %.0fms",
                          step.name, step.anchor, conf, el)

            if step.action == "arrive":
                return ARRIVED, {"step": step.name, "conf": conf}
            if step.action == "halt":
                return HALT, {"step": step.name, "reason": step.note}
            if step.action == "wait":
                return WORKING, {"step": step.name, "waited": True}
            if step.action == "click":
                return self._click(step, m, conf, frame_gray)

        el = (time.time() - t0) * 1000
        self.unknown_streak += 1
        self.log.info("resume: no anchor matched (%.0fms, streak %d)",
                      el, self.unknown_streak)
        return WORKING, {"step": "unknown", "streak": self.unknown_streak}

    def _click(self, step, match, conf, frame_gray):
        target = step.target
        if target in self.never_click:
            self.log.error("resume: BLOCKED click on %s (never-click list)", target)
            return HALT, {"step": step.name,
                          "reason": f"{target} is on the never-click list"}

        # Re-resolve the click point from the TARGET template when it differs
        # from the anchor, so we never click one thing merely because another
        # matched. Only the offset path is allowed to derive a point from a
        # neighbouring template, and that is why the offset steps sit last in the
        # ladder.
        if target != step.anchor:
            t = self.templates.get(target)
            if t is None:
                return HALT, {"step": step.name,
                              "reason": f"no template for target {target}"}
            saved = (t.scales, t.threshold)
            try:
                if step.target_scales:
                    t.scales = step.target_scales
                if step.target_threshold is not None:
                    t.threshold = step.target_threshold
                m2, c2 = find(frame_gray, t)
            finally:
                t.scales, t.threshold = saved
            if not m2.found:
                self.log.warning("resume: %s matched but target %s is absent "
                                 "(conf %.3f); not clicking",
                                 step.anchor, target, c2)
                return WORKING, {"step": step.name, "target_absent": target}
            match, conf = m2, c2

        x = match.center[0] + step.offset[0]
        y = match.center[1] + step.offset[1]
        self.actor.click_pixel(x, y, why=f"resume:{step.name}")
        return WORKING, {"step": step.name, "clicked": (x, y), "conf": conf}

    # -- drive ---------------------------------------------------------------
    def run(self, timeout=180, max_unknown=20, settle=1.0):
        """Climb until ARRIVED / HALT / STOPPED, or give up.

        `max_unknown` bounds the case the reference bot cannot hit and we can: an
        unrecognised screen. Theirs has a pixel anchor for every screen it cares
        about; ours has templates for some, so "no anchor matched" is a real
        outcome and must be bounded rather than spun on.
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            outcome, info = self.advance()
            if outcome in (ARRIVED, HALT, STOPPED):
                self.log.info("resume: %s after %.1fs %s",
                              outcome, time.time() - t0, info)
                return outcome, info
            if self.unknown_streak >= max_unknown:
                self.log.error("resume: %d consecutive unrecognised frames; "
                               "halting rather than clicking blindly",
                               self.unknown_streak)
                return HALT, {"reason": "unrecognised screen",
                              "streak": self.unknown_streak}
            time.sleep(settle)
        self.log.error("resume: timed out after %ss", timeout)
        return HALT, {"reason": "timeout"}
