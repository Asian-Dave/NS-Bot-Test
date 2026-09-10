#!/usr/bin/env python3
"""Recut a template for a Ruffle backend that draws it differently, and
CALIBRATE the crop instead of eyeballing it.

WHY THIS EXISTS
---------------
The same SWF does not produce the same pixels on every Ruffle backend.
Measured on one screen, same session, minutes apart, only `renderMode` changed:

    mission_room_entry   wgpu-webgl 0.997   webgl 0.531   (gate 0.88)
    char_slot_level      wgpu-webgl 0.965   webgl 0.677   canvas 0.678
    character_select     wgpu-webgl 0.865   webgl 0.442   canvas 0.436

The split is not "legacy webgl is broken": webgl and canvas agree with each
other to 0.001 and both disagree with the wgpu backend, which draws text WITH
its stroke where the other two draw a thinner, unoutlined face at a different
baseline. Every template in this project was cut against wgpu-webgl, so the
project is calibrated against the outlier.

WHY A NAIVE RECUT IS NOT ENOUGH - and this is the whole point of the tool.
Cutting the same region out of a webgl frame gives a template that matches its
own frame at 1.000 and then FALSE-POSITIVES everywhere:

    mission_room_entry, same region, webgl:  self 1.000, worst negative 0.907

against a 0.88 gate. Strip the stroked text out of that plaque and what is left
is smooth pale pink, and a low-variance template correlates with any large
smooth region - the same trap this project already recorded for
`close_popup_x_large` ("flat 0.547 at every scale. Bad crop") and for the
flood-filled digit masks ("degenerate near-uniform masks").

The fix is to give the crop back some STRUCTURE by expanding it into the
surrounding art, and the expansion must be measured:

    pad    self   worst-neg   margin
      0   1.000       0.907    +0.093     unusable
     20   1.000       0.780    +0.220
     40   1.000       0.665    +0.335
     80   1.000       0.618    +0.382
    110   1.000       0.547    +0.453

EXPAND SYMMETRICALLY ABOUT THE MATCH CENTRE, always. The centre is what gets
CLICKED, so an asymmetric crop silently moves every click that anchor drives -
and this project has already paid for a half-applied coordinate correction
("one coordinate space, or none"). Symmetric padding leaves the centre exactly
where it was, which the tool asserts rather than assumes.

Two limits, stated because they bound what the output is worth:

  * The negative set is the committed `ref/` frames, which are wgpu-rendered.
    For a crop dominated by ART that is valid - the art measured
    pixel-identical between backends (mean |diff| 3..7, the whole difference
    confined to the text rows). It is NOT valid for a text-dominated crop, so
    the tool reports how much of the chosen crop is text-band.
  * A single source frame says nothing about VARIANCE. Pass several frames of
    the same screen (`--frames dir/`) and the worst one decides, because the
    Go! badge scored 0.845 on two frames in seven and 0.326 on the other five,
    and one sample cost a live loop.

USAGE
-----
    # where does the default template match on a frame of this screen?
    python engine/recut.py locate mission_room_entry --frames ref/auto/lobby

    # cut and calibrate for a backend, writing tpl/<renderer>/<name>.png
    python engine/recut.py cut mission_room_entry webgl \
        --frames run/harvest/lobby --at 1067,577

    # score what is already there
    python engine/recut.py check mission_room_entry webgl \
        --frames run/harvest/lobby
"""
import argparse
import glob
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import perceive  # noqa: E402
from perceive import Template, find  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Expansions tried, in template units. 0 is included so the report always shows
# how bad the naive same-region cut is - that number is the argument for the
# rest of the tool.
PADS = (0, 20, 40, 60, 80, 110, 140)

# ACCEPTANCE, and getting this wrong made the tool reject perfectly good crops
# on its first run. The first attempt scored `self - max(neg, threshold)`,
# which for any crop whose negatives sit below the gate is just
# `self - threshold` - a CONSTANT. Every expansion of `char_slot_level`
# reported "+0.120" and nothing could ever qualify, while pad 0 was in fact
# fine (self 1.000, worst negative 0.640).
#
# Two independent conditions, because they answer different questions:
#
#   does it recognise its own screen?   worst self-match >= threshold
#   will it fire on the wrong screen?   best negative <= threshold - slack
#
# The bar is set from what this project's REAL anchors achieve, not from the
# smallest number that is arguably safe. Every discrimination margin recorded
# in CLAUDE.md sits between 0.37 and 0.66 - `page_next` 0.973 against 0.600,
# `action_flag` 0.897 against 0.255, `level_up` 1.000 against 0.508. A crop
# whose best negative creeps to 0.78 against an 0.88 gate is nothing like
# those, even though 0.78 is technically under the gate.
#
# So: negatives at or below `threshold - 0.18`, and at least 0.30 of clear air
# between the worst self-match and the best negative. Calibrate to the company
# the anchor keeps.
NEG_SLACK = 0.18
MIN_SEPARATION = 0.30

# Among the crops that qualify, prefer the SMALLEST expansion. A bigger crop
# leans on more surrounding art, and surrounding art is exactly what changes
# when the game draws an event banner, a level-up flourish or a different
# time of day - which is why the command buttons had to be re-cut TIGHT after
# a night map destroyed the wide crops that carried the map behind them.


class _Log:
    def info(self, m, *a):
        print("  " + ((m % a) if a else m))
    warning = error = info


def _frames(spec):
    """Load frames from a file, a directory, or a comma-separated list."""
    paths = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if os.path.isdir(part):
            paths += sorted(glob.glob(os.path.join(part, "*.png")))
        else:
            paths.append(part)
    out = []
    for p in paths:
        im = cv2.imread(p)
        if im is None:
            print(f"  (unreadable, skipped: {p})")
            continue
        out.append((p, im))
    if not out:
        sys.exit(f"no readable frames in {spec!r}")
    return out


def _negatives(exclude, anchor=None):
    """Committed reference frames, as the false-positive set.

    Anything under `ref/` that is not one of the source frames. These are
    wgpu-rendered; see the note in the module docstring for what that bounds.

    A FRAME SHOWING THE SAME ANCHOR IS NOT A NEGATIVE, and forgetting that
    rejected a perfectly good crop. `special_tab` recut for webgl scored 1.000
    on its own frame and was refused because three committed frames read
    0.795 - and those three were `mission/room_00`, `mission/room_05` and
    `tp/room`, every one of them a wgpu Mission Room WITH THE SPECIAL TAB ON
    IT. The webgl variant was being penalised for recognising its own anchor in
    the other rendering, which is not something that can ever happen: a
    variant is only ever loaded under its own backend.

    So when `anchor` is given, any frame the DEFAULT template already matches
    is dropped. That is a positive-in-another-rendering by definition, and it
    is measured rather than listed by hand.
    """
    keep = {os.path.abspath(p) for p in exclude}
    dropped = []
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "ref/**/*.png"), recursive=True)):
        if os.path.abspath(p) in keep:
            continue
        # `ref/auto/renderer/` holds per-backend POSITIVES - committed frames
        # of a given screen under a given renderer, kept as fixtures. Scoring
        # them as negatives would report a perfect self-match as the worst
        # false positive and reject every crop, which is exactly what happened
        # the moment the first webgl fixture was committed.
        if os.path.sep + os.path.join("ref", "auto", "renderer") + os.path.sep in p:
            continue
        im = cv2.imread(p)
        if im is None:
            continue
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        if anchor is not None:
            try:
                _, c = find(g, anchor)
            except Exception:
                c = -1.0
            if c >= anchor.threshold:
                dropped.append((p, c))
                continue
        out.append((p, g))
    if dropped:
        print(f"  {len(dropped)} frame(s) dropped from the negatives - they "
              f"SHOW this anchor in the other rendering:")
        for p, c in dropped[:6]:
            print(f"      {c:.3f}  {os.path.relpath(p, ROOT)}")
    return out


def _default_template(name):
    cfg_path = os.path.join(ROOT, "Configs/mission.json")
    cfg = json.load(open(cfg_path))
    spec = (cfg.get("templates") or {}).get(name)
    if spec:
        path = os.path.join(ROOT, spec["path"])
        thr = spec.get("threshold", 0.88)
    else:
        path = os.path.join(ROOT, "tpl", f"{name}.png")
        thr = 0.88
    if not os.path.exists(path):
        sys.exit(f"no default template for {name!r} at {path}")
    return Template(name, path, threshold=thr), path, thr


def _score_crop(crop, name, thr, grays, negs):
    """(worst self-match, best false positive, the match centre) for one crop."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    cv2.imwrite(path, crop)
    try:
        tp = Template(name, path, threshold=thr)
        selfs, centre = [], None
        for g in grays:
            m, c = find(g, tp)
            selfs.append(c)
            if m.center:
                centre = m.center
        worst_neg, where = -1.0, ""
        for p, g in negs:
            try:
                _, c = find(g, tp)
            except Exception:
                continue
            if c > worst_neg:
                worst_neg, where = c, p
        return min(selfs), max(selfs), worst_neg, where, centre
    finally:
        os.unlink(path)


def cmd_locate(a):
    """Where does the DEFAULT template match on these frames, and how well?

    Run this on frames of the backend that WORKS to get the centre to recut
    about, then pass that centre to `cut` with frames of the broken backend.
    """
    tp, path, thr = _default_template(a.name)
    print(f"{a.name}: {os.path.relpath(path, ROOT)} "
          f"{tp.w}x{tp.h}, threshold {thr}")
    for p, im in _frames(a.frames):
        m, c = find(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), tp)
        print(f"  {c:.3f} {'MATCH' if c >= thr else 'no   '} at {m.center}"
              f"   {os.path.relpath(p, ROOT)}")


def cmd_cut(a):
    tp, path, thr = _default_template(a.name)
    src = _frames(a.frames)
    grays = [cv2.cvtColor(im, cv2.COLOR_BGR2GRAY) for _, im in src]
    negs = _negatives([p for p, _ in src], anchor=tp)
    print(f"{a.name} for {a.renderer}: {len(src)} source frame(s), "
          f"{len(negs)} negative(s), gate {thr}\n")

    if a.at:
        cx, cy = (int(v) for v in a.at.split(","))
    else:
        # No centre given: take it from the default template's own best match
        # on the source frames. That only works when the default still matches
        # there, which for a broken backend it does not - hence --at.
        m, c = find(grays[0], tp)
        if not m.center:
            sys.exit("could not locate the anchor on the source frames; pass "
                     "--at X,Y taken from `recut.py locate` on a frame of the "
                     "backend that works")
        cx, cy = m.center
        print(f"  centre from the default template's own match: ({cx},{cy}) "
              f"at {c:.3f}")
    print(f"  recutting about ({cx},{cy}); the click must land here\n")

    h, w = tp.h, tp.w
    fh, fw = src[0][1].shape[:2]
    ceiling = thr - NEG_SLACK
    print(f"  self-match must reach {thr:.2f}; "
          f"negatives must stay at or below {ceiling:.2f}, separation >= {MIN_SEPARATION:.2f}\n")
    print(f"  {'pad':>5}{'size':>12}{'self worst':>12}{'best':>8}"
          f"{'worst-neg':>11}{'separation':>12}   verdict")
    best = None
    for pad in PADS:
        cw, ch = w + 2 * pad, h + 2 * pad
        x0, y0 = cx - cw // 2, cy - ch // 2
        if x0 < 0 or y0 < 0 or x0 + cw > fw or y0 + ch > fh:
            print(f"  {pad:>5}  runs off the frame")
            continue
        crop = src[0][1][y0:y0 + ch, x0:x0 + cw]
        lo, hi, neg, where, centre = _score_crop(crop, a.name, thr, grays, negs)
        centred = centre == (cx, cy)
        why = []
        if not centred:
            why.append(f"centre MOVED to {centre}")
        if lo < thr:
            why.append("misses its own screen")
        if neg > ceiling:
            why.append("negatives too close")
        elif lo - neg < MIN_SEPARATION:
            why.append(f"separation only {lo - neg:+.3f}")
        verdict = "; ".join(why) if why else "QUALIFIES"
        print(f"  {pad:>5}{f'{cw}x{ch}':>12}{lo:>12.3f}{hi:>8.3f}"
              f"{neg:>11.3f}{lo - neg:>+12.3f}   {verdict}")
        if not why and best is None:
            best = (pad, crop, lo, hi, neg, where)
    print()
    if best is None:
        print("  NOTHING QUALIFIED. Every expansion either misses its own "
              "screen, fires on the wrong one, or moves the match centre.")
        print("  DO NOT lower the gate to force this through: a broken anchor "
              "scores inside the negative distribution (0.531 against "
              "negatives of 0.39..0.53 on the same frame), so a lower gate "
              "buys false positives rather than recognition.")
        return 1
    pad, crop, lo, hi, neg, where = best
    out = os.path.join(ROOT, perceive.VARIANT_DIR, a.renderer, f"{a.name}.png")
    if a.dry_run:
        print(f"  would write {os.path.relpath(out, ROOT)} at pad {pad}")
        return 0
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cv2.imwrite(out, crop)
    print(f"  wrote {os.path.relpath(out, ROOT)}  "
          f"{crop.shape[1]}x{crop.shape[0]} (pad {pad}, the smallest that "
          f"qualifies)")
    print(f"    self {lo:.3f}..{hi:.3f} over {len(grays)} frame(s), "
          f"worst negative {neg:.3f} ({os.path.relpath(where, ROOT)})")
    print(f"    separation {lo - neg:+.3f}")
    print(f"    match centre stays ({cx},{cy}), so the click does not move")
    return 0


def cmd_free(a):
    """Search sub-windows for a DETECTOR-ONLY anchor, where the centre is
    never clicked - so the crop need not stay symmetric.

    WHY THIS MODE EXISTS. `result_panel` cannot be fixed by symmetric
    expansion: every pad from 0 to 140 left its best unrelated negative
    between 0.70 and 0.85 against an 0.88 gate, because the panel is a large
    flat parchment banner and expanding it only adds more flat parchment.

        pad     0  self 1.000  worst-neg 0.849
        pad    40  self 1.000  worst-neg 0.702
        pad   140  self 0.995  worst-neg 0.745

    Symmetry is only required because the match CENTRE gets clicked. For
    `result_panel` it does not: `resume.py` uses it as a rung ANCHOR whose
    clicked target is `mission_start`, and CLAUDE.md records that the panel
    body is not a hit area at all - a mid-mission Victory absorbed eleven
    clicks at the canvas centre. So for anchors like this the whole panel
    region is available and the crop can be chosen for discrimination alone.

    THE CALLER MUST SAY SO EXPLICITLY (`--no-click-anchor`). Using this on an
    anchor whose centre IS clicked would move every click it drives, silently.
    """
    tp, path, thr = _default_template(a.name)
    src = _frames(a.frames)
    grays = [cv2.cvtColor(im, cv2.COLOR_BGR2GRAY) for _, im in src]
    negs = _negatives([p for p, _ in src], anchor=tp)
    cx, cy = (int(v) for v in a.at.split(","))
    fh, fw = src[0][1].shape[:2]
    ceiling = thr - NEG_SLACK
    print(f"{a.name} for {a.renderer}: FREE search (detector-only anchor)")
    print(f"  {len(src)} source frame(s), {len(negs)} negative(s), gate {thr}")
    print(f"  negatives must stay at or below {ceiling:.2f}, "
          f"separation >= {MIN_SEPARATION:.2f}\n")

    # CANDIDATES MUST LIE INSIDE THE ANCHOR'S OWN FOOTPRINT.
    #
    # The first version searched a band of +/- one template height around the
    # centre, and it picked a window that was NOT THE PANEL AT ALL: the team
    # badge reading "0/2" plus sky and cloud art from above the banner. Two
    # separate faults in one crop -
    #
    #   * it contains a COUNTER, and this project's own rule is that an anchor
    #     must not contain the thing that varies (`tp_seal_hud` was re-cut for
    #     exactly this, after "Skill : 1 / 4" stopped matching at "2 / 5");
    #   * it would match in ANY battle, not only when a result panel is up, so
    #     it does not detect the state it is named after.
    #
    # It scored a +0.576 separation regardless, because the negatives are all
    # wgpu frames and that HUD renders differently there - a separation earned
    # against the wrong question. Constraining candidates to sub-rectangles of
    # the default template makes every crop structurally a piece of the anchor,
    # which is the property the score cannot check for itself.
    w, h = tp.w, tp.h
    ax0, ay0 = cx - w // 2, cy - h // 2
    cands = []
    for fx in (1.0, 0.6, 0.45, 0.3):
        for fy in (1.0, 0.7, 0.5):
            sw, sh = int(w * fx), int(h * fy)
            if sw < 40 or sh < 32:
                continue
            for x0 in range(ax0, ax0 + w - sw + 1, max(1, sw // 2)):
                for y0 in range(ay0, ay0 + h - sh + 1, max(1, max(8, sh // 2))):
                    if x0 < 0 or y0 < 0 or x0 + sw > fw or y0 + sh > fh:
                        continue
                    cands.append((x0, y0, sw, sh))
    # de-duplicate
    cands = sorted(set(cands))
    print(f"  {len(cands)} candidate window(s)")

    # Two passes: a cheap one over a SAMPLE of the negatives to rank, then a
    # full check on the leaders. Scoring every candidate against every negative
    # would be thousands of full-frame matches.
    sample = negs[::max(1, len(negs) // 24)]
    ranked = []
    for (x0, y0, sw, sh) in cands:
        crop = src[0][1][y0:y0 + sh, x0:x0 + sw]
        lo, hi, neg, _, _ = _score_crop(crop, a.name, thr, grays, sample)
        if lo < thr:
            continue
        ranked.append((lo - neg, x0, y0, sw, sh, lo, neg))
    ranked.sort(reverse=True)
    print(f"  {len(ranked)} matched their own frames; verifying the best 5 "
          f"against all {len(negs)} negatives\n")
    print(f"  {'window':>22}{'self':>8}{'worst-neg':>11}{'separation':>12}"
          f"   verdict")
    best = None
    for _, x0, y0, sw, sh, _, _ in ranked[:5]:
        crop = src[0][1][y0:y0 + sh, x0:x0 + sw]
        lo, hi, neg, where, _ = _score_crop(crop, a.name, thr, grays, negs)
        why = []
        if lo < thr:
            why.append("misses its own screen")
        if neg > ceiling:
            why.append("negatives too close")
        elif lo - neg < MIN_SEPARATION:
            why.append(f"separation only {lo - neg:+.3f}")
        verdict = "; ".join(why) if why else "QUALIFIES"
        print(f"  {f'{sw}x{sh} at ({x0},{y0})':>22}{lo:>8.3f}{neg:>11.3f}"
              f"{lo - neg:>+12.3f}   {verdict}")
        if not why and (best is None or lo - neg > best[0]):
            best = (lo - neg, crop, lo, neg, where, x0, y0, sw, sh)
    print()
    if best is None:
        print("  NOTHING QUALIFIED even with a free crop. This anchor needs a "
              "different mechanism, not a different rectangle.")
        return 1
    sep, crop, lo, neg, where, x0, y0, sw, sh = best
    out = os.path.join(ROOT, perceive.VARIANT_DIR, a.renderer, f"{a.name}.png")
    if a.dry_run:
        print(f"  would write {os.path.relpath(out, ROOT)}  {sw}x{sh}")
        return 0
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cv2.imwrite(out, crop)
    print(f"  wrote {os.path.relpath(out, ROOT)}  {sw}x{sh} at ({x0},{y0})")
    print(f"    self {lo:.3f}, worst negative {neg:.3f} "
          f"({os.path.relpath(where, ROOT)}), separation {sep:+.3f}")
    print(f"    DETECTOR ONLY - nothing clicks this anchor's centre")
    return 0


def cmd_check(a):
    """Score an existing variant against its source frames and the negatives."""
    tp, path, thr = _default_template(a.name)
    var = os.path.join(ROOT, perceive.VARIANT_DIR, a.renderer, f"{a.name}.png")
    if not os.path.exists(var):
        sys.exit(f"no variant at {os.path.relpath(var, ROOT)}")
    src = _frames(a.frames)
    grays = [cv2.cvtColor(im, cv2.COLOR_BGR2GRAY) for _, im in src]
    negs = _negatives([p for p, _ in src], anchor=tp)
    vt = Template(a.name, var, threshold=thr)
    print(f"{a.name} / {a.renderer}: {vt.w}x{vt.h}, gate {thr}")
    for lbl, t in ((f"default ({tp.w}x{tp.h})", tp), (f"variant ({vt.w}x{vt.h})", vt)):
        lo = min(find(g, t)[1] for g in grays)
        hi = max(find(g, t)[1] for g in grays)
        print(f"  {lbl:<22} self {lo:.3f}..{hi:.3f}  "
              f"{'MATCHES' if lo >= thr else 'no'}")
    worst, where = -1.0, ""
    for p, g in negs:
        try:
            _, c = find(g, vt)
        except Exception:
            continue
        if c > worst:
            worst, where = c, p
    print(f"  worst false positive  {worst:.3f}  {os.path.relpath(where, ROOT)}"
          f"  {'*** OVER THE GATE ***' if worst >= thr else ''}")
    m, _ = find(grays[0], vt)
    print(f"  match centre {m.center}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("locate", help="where the default template matches")
    p.add_argument("name")
    p.add_argument("--frames", required=True)
    p.set_defaults(fn=cmd_locate)

    p = sub.add_parser("cut", help="cut and calibrate a per-renderer variant")
    p.add_argument("name")
    p.add_argument("renderer")
    p.add_argument("--frames", required=True,
                   help="file, directory or comma-separated list of frames of "
                        "THIS renderer showing the anchor")
    p.add_argument("--at", help="X,Y centre to recut about, from `locate` on "
                                "a frame of the renderer that works")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_cut)

    p = sub.add_parser("free", help="search sub-windows for a detector-only "
                                    "anchor (centre never clicked)")
    p.add_argument("name")
    p.add_argument("renderer")
    p.add_argument("--frames", required=True)
    p.add_argument("--at", required=True,
                   help="X,Y of the anchor, to search around")
    p.add_argument("--no-click-anchor", action="store_true", required=True,
                   help="REQUIRED acknowledgement that nothing clicks this "
                        "anchor's match centre, so an asymmetric crop is safe")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_free)

    p = sub.add_parser("check", help="score an existing variant")
    p.add_argument("name")
    p.add_argument("renderer")
    p.add_argument("--frames", required=True)
    p.set_defaults(fn=cmd_check)

    a = ap.parse_args()
    perceive.clear_search_band()
    perceive.clear_renderer()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
