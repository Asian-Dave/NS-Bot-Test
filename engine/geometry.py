"""Battle geometry — where the slots are, derived not assumed.

WHY THIS MODULE EXISTS
----------------------
The reference bot (`ref/tp/cmmhero`) stores every battle coordinate as an
absolute pixel pair: `skillPositions["S1"] = (225, 374)`, `T1 = (353, 169)`, and
so on for ~2,500 constants. It can do that because it *forces* the game window to
one size and reparents it, so exactly one geometry ever occurs.

We cannot. Our canvas geometry varies — measured on our own recorded frames, the
command bar appears at scale 0.46 in one capture set and 0.545 in another, an 18%
difference (CLAUDE.md records this as the "known scale problem"). Copying their
numbers would silently miss every slot, and text-heavy templates lose ~0.4
confidence at 8% scale error, so no threshold tuning survives it.

So the same tables become ANCHOR-RELATIVE. We locate the command bar by template
match — which also hands us the observed scale for free — and compute every slot
from an offset expressed in scale-independent "template units":

    captured_px = anchor_px + offset_template_units * observed_scale

MEASURED, AND VALIDATED ACROSS GEOMETRIES
-----------------------------------------
Offsets below were measured on ref/combat/t0_after_first_attack.jpg (anchor
charge_btn at (419,443), scale 0.46), then used to PREDICT positions in
ref/combat/boss_t2.jpg (anchor (764,460), scale 0.545). Result: all 8 target
slots landed on their real borders, confirmed by border colour, and the skill
slots landed within ~6px of borders found by an independent projection profile.

Two facts fell out of that measurement and both are load-bearing:

  * The two geometries are a PURE UNIFORM SCALE. Command-bar pitch / match scale
    was 59/0.545 = 108.3 and 50/0.46 = 108.7 — agreeing to 0.4%. That is why one
    scalar is enough and no aspect correction is needed.
  * The command bar is a 2x2 block of side 108.7 template units, not a row.
    Attack top-left, Dodge top-right, Charge bottom-left, Run bottom-right.

THE TARGET RING — resolves an open question in CLAUDE.md
--------------------------------------------------------
CLAUDE.md records targeting as UNRESOLVED: clicking an enemy sprite worked once,
name plates worked once, and later neither did. The reference bot explains why —
it never clicks art at all. It clicks eight fixed battlefield slots.

That ring exists on OUR client too. It is an 8-slot grid around the battle
centre, and it is persistent (an earlier probe of mine reported it "transient"
purely because I probed one geometry's coordinates against another's frames).
Measured border colours give the teams away:

    T1..T4  RED  border  -> enemy side   (upper arc)
    T5..T8  YELLOW border -> ally side   (lower arc)

Ordering matches the reference bot's own T1..T8 exactly: outer-left, inner-left,
inner-right, outer-right across the top, then the same across the bottom.

STILL UNVERIFIED — do not treat as fact
---------------------------------------
That clicking a ring slot actually *selects* that target is inferred from the
reference bot's behaviour plus the ring's existence here. It has NOT been
confirmed against our live client. `BattleGeometry.target()` gives you the point;
proving the click lands is a live-run task. See `docs/UI_MAP.md` when it is.
"""
import cv2

# --- offsets in template units, relative to the charge_btn centre -----------
# Derived from the measurement described above. Regularised where the raw
# measurement was within noise of an exact figure: the command bar is a true
# square (108.7), the ring is symmetric about dx=+48.5, and the skill banks use
# a uniform pitch (raw per-slot pitch ran 100..106.5, i.e. +-6 units, which is
# +-3px at scale 0.46 — border-midpoint noise, not real irregularity).

CMD_SIDE = 108.7

COMMAND = {
    "AT": (0.0, -CMD_SIDE),        # Attack  top-left
    "DO": (CMD_SIDE, -CMD_SIDE),   # Dodge   top-right
    "CH": (0.0, 0.0),              # Charge  bottom-left  == the anchor itself
    "RN": (CMD_SIDE, 0.0),         # Run     bottom-right
}

# Skill banks: 4 left of the command bar, 4 right. Uniform 103-unit pitch.
_SKILL_PITCH = 103.0
_SKILL_Y = -13.0                   # slot centres sit slightly above the anchor
SKILLS = {
    **{f"S{i+1}": (-427.0 + i * _SKILL_PITCH, _SKILL_Y) for i in range(4)},
    **{f"S{i+5}": (224.0 + i * _SKILL_PITCH, _SKILL_Y) for i in range(4)},
}

# Target ring: symmetric about dx=+48.5. Inner columns +-55.5, outer +-144.5.
_RING_CX = 48.5
_RING_IN, _RING_OUT = 55.5, 144.5
_ROW_TOP, _ROW_UP, _ROW_LOW, _ROW_BOT = -615.0, -516.5, -411.0, -320.0
TARGETS = {
    "T1": (_RING_CX - _RING_OUT, _ROW_UP),    # enemy  outer left
    "T2": (_RING_CX - _RING_IN, _ROW_TOP),    # enemy  inner left
    "T3": (_RING_CX + _RING_IN, _ROW_TOP),    # enemy  inner right
    "T4": (_RING_CX + _RING_OUT, _ROW_UP),    # enemy  outer right
    "T5": (_RING_CX - _RING_OUT, _ROW_LOW),   # ally   outer left
    "T6": (_RING_CX - _RING_IN, _ROW_BOT),    # ally   inner left
    "T7": (_RING_CX + _RING_IN, _ROW_BOT),    # ally   inner right
    "T8": (_RING_CX + _RING_OUT, _ROW_LOW),   # ally   outer right
}

ENEMY_SLOTS = ("T1", "T2", "T3", "T4")
ALLY_SLOTS = ("T5", "T6", "T7", "T8")

# Ring border colours, in HSV, as measured. Used to tell an occupied/enemy slot
# from an ally slot without clicking anything.
_RED_H = ((0, 10), (170, 180))
_YEL_H = (20, 35)
_MIN_BORDER_PX = 60               # measured 254..667 for a real slot, 0..11 for none


class BattleGeometry:
    """Slot positions for one frame, in CAPTURED PIXELS.

    Captured pixels are what the matcher returns and what `Actor.click_pixel`
    expects, so nothing here needs to know about CSS or the device pixel ratio —
    that conversion lives in `Capture.to_click_coords` and nowhere else.
    """

    def __init__(self, anchor, scale, confidence=None):
        self.anchor = anchor
        self.scale = float(scale)
        self.confidence = confidence

    # -- construction --------------------------------------------------------
    @classmethod
    def locate(cls, frame_gray, charge_tpl, dodge_tpl, scales=None,
               min_conf=0.70, pitch_tolerance=0.12):
        """Find the command bar and derive the geometry, or return None.

        Gates on charge_btn AND dodge_btn together. CLAUDE.md's discrimination
        matrix is explicit about why: individually the command buttons vary
        (charge 0.949, dodge 0.918, run 0.940, attack only 0.791), and attack_btn
        is too weak to gate on at all. Two corroborating buttons are unambiguous.

        `min_conf` is 0.70, NOT the 0.85 the single-geometry matrix suggests, and
        that is deliberate. Our templates were cut at scale 0.46; on the 0.545
        capture set the same real command bar only reaches 0.746/0.788, because
        matchTemplate is not scale invariant. An 0.85 gate silently classified
        every boss-encounter frame as "not combat".

        Measured separation over our recorded frames:
            command bar present : 0.746 .. 0.949
            no command bar      : 0.407 .. 0.470  (lobby, epilogue)
        The gap is 0.276 wide, so 0.70 sits inside it with room on both sides.
        The geometric cross-check below carries the rest of the load.

        The pair also cross-checks itself. Dodge must sit one CMD_SIDE to the
        RIGHT of and one ABOVE charge; if the measured pitch disagrees with the
        matched scale by more than `pitch_tolerance`, the two matches are not
        really the same command bar and we return None rather than build a
        geometry on top of a coincidence. On our epilogue frames — which have no
        command bar — the two templates match 340px apart at wildly different
        scales, and this is the check that rejects them.
        """
        scales = scales or [round(0.30 + i * 0.01, 2) for i in range(90)]
        ch = _best(frame_gray, charge_tpl, scales)
        do = _best(frame_gray, dodge_tpl, scales)
        if ch is None or do is None:
            return None
        if ch[0] < min_conf or do[0] < min_conf:
            return None

        conf_ch, s_ch, (cx, cy) = ch
        conf_do, s_do, (dx, dy) = do
        scale = (s_ch + s_do) / 2.0
        expect = CMD_SIDE * scale
        if expect <= 0:
            return None
        # dodge is right of and above charge by exactly one side length
        err = max(abs((dx - cx) - expect), abs((cy - dy) - expect)) / expect
        if err > pitch_tolerance:
            return None
        return cls((cx, cy), scale, confidence=min(conf_ch, conf_do))

    # -- lookups -------------------------------------------------------------
    def _at(self, table, key):
        ox, oy = table[key]
        return (int(round(self.anchor[0] + ox * self.scale)),
                int(round(self.anchor[1] + oy * self.scale)))

    def cmd(self, key):
        """Command button centre. key in AT / DO / CH / RN."""
        return self._at(COMMAND, key)

    def slot(self, key):
        """Skill slot centre. key in S1..S8."""
        return self._at(SKILLS, key)

    def target(self, key):
        """Target ring slot centre. key in T1..T8."""
        return self._at(TARGETS, key)

    def slot_box(self, key, size=44.0):
        """(x, y, w, h) around a skill slot, for saturation / cooldown reads."""
        cx, cy = self.slot(key)
        s = int(round(size * self.scale / 0.46))   # 44px was measured at scale .46
        return (cx - s // 2, cy - s // 2, s, s)

    # -- ring inspection -----------------------------------------------------
    def ring_state(self, frame_bgr, probe=24):
        """Which ring slots are present, and whose side each is on.

        Returns {slot: 'enemy' | 'ally' | None}. Reads the BORDER colour, not the
        interior, because the interior is flat brown whether the slot holds a
        combatant or not — measured red 254..667 px for a real bordered slot
        versus 0..11 for empty background, so the separation is wide and a fixed
        pixel count is safe here.

        This is a cheap way to answer "how many enemies are there" without
        scanning for HP bars, and it does not depend on enemy name plates, which
        CLAUDE.md already rules out as unusable (names vary per encounter).
        """
        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        h, w = hsv.shape[:2]
        out = {}
        for key in TARGETS:
            x, y = self.target(key)
            r = int(round(probe * self.scale / 0.46))
            y0, y1 = max(0, y - r), min(h, y + r)
            x0, x1 = max(0, x - r), min(w, x + r)
            box = hsv[y0:y1, x0:x1]
            if box.size == 0:
                out[key] = None
                continue
            hue, sat, val = box[:, :, 0], box[:, :, 1], box[:, :, 2]
            strong = (sat > 120) & (val > 90)
            red = (((hue < _RED_H[0][1]) | (hue > _RED_H[1][0])) & strong).sum()
            yel = ((hue > _YEL_H[0]) & (hue < _YEL_H[1]) & (sat > 120) & (val > 120)).sum()
            if red >= _MIN_BORDER_PX and red >= yel:
                out[key] = "enemy"
            elif yel >= _MIN_BORDER_PX:
                out[key] = "ally"
            else:
                out[key] = None
        return out

    def enemy_targets(self, frame_bgr):
        """Ring slots on the enemy side that are actually drawn, left to right."""
        st = self.ring_state(frame_bgr)
        return [k for k in ENEMY_SLOTS if st.get(k) == "enemy"]

    def __repr__(self):
        return (f"<BattleGeometry anchor={self.anchor} scale={self.scale:.3f} "
                f"conf={self.confidence}>")


def _best(frame_gray, tpl, scales):
    """Best (confidence, scale, centre) for one template over a scale sweep."""
    best = None
    fh, fw = frame_gray.shape[:2]
    for s in scales:
        th, tw = int(tpl.h * s), int(tpl.w * s)
        if th < 6 or tw < 6 or th > fh or tw > fw:
            continue
        small = cv2.resize(tpl.gray, (tw, th),
                           interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
        res = cv2.matchTemplate(frame_gray, small, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if best is None or mx > best[0]:
            best = (float(mx), s, (loc[0] + tw // 2, loc[1] + th // 2))
    return best
