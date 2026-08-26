#!/usr/bin/env python3
"""Turn a game asset into a template. Pixel-perfect source, no screenshotting.

WHERE THE ASSETS CAME FROM
--------------------------
`ref/swf_assets/` holds 68 bitmaps extracted from the game's OWN client SWF.
Provenance, so this is reproducible:

    ninja_saga.exe (100MB)          Qt self-extracting installer
      -> overlay at 1,899,541       marker "IFSETUP_START" (bytes +1) + 7z
      -> 7z payload                 filenames are base64(UTF-16LE)
      -> files/air.swf (21KB)       the ENTIRE AIR app: a CEF webview wrapper
      -> launcher.json              https://ninjasaga.cc/launcher.json
      -> ninja_saga.swf (2.87MB)    https://cdn.ninjasaga.cc/cdn/swf/latest/
      -> 68 DefineBits* tags        extracted to ref/swf_assets/

The SWF also gave us the authoritative stage size: **960x720 at 24fps**, which
confirms what CLAUDE.md had measured by hand.

WHY ASSET-DERIVED TEMPLATES ARE BETTER
--------------------------------------
A screenshot crop carries JPEG noise, the background behind the element, and
whatever hover state happened to be active. The SWF bitmap is the exact source
art with a real alpha channel.

THE RECIPE, AND WHY EACH STEP
-----------------------------
1. Load the asset WITH alpha.
2. Crop to the largest fully-opaque axis-aligned box.
3. Scale to on-screen size (calibrated once against a real frame).
4. Save OPAQUE.

Step 2 is the important one and it is not obvious. You could instead keep the
alpha and match with a mask — `perceive.Template` supports that. But masked
matching has to use TM_CCORR_NORMED, which has a badly inflated baseline.
Measured on the green check:

    composited onto white, CCOEFF   0.797 positive
    full alpha + mask, CCORR        0.944 positive / 0.817-0.834 negative  (margin 0.11)
    opaque core, CCOEFF            *0.958 positive / 0.485-0.576 negative  (margin 0.38)

So cropping to the opaque core lets us stay on the normal CCOEFF path, where a
0.88 threshold means the same thing it means for every other template, and the
margin is 3x wider. Masked matching stays available for assets with no usable
opaque core (thin glyphs, outlines).

Step 3 needs calibration because Flash applies timeline transforms: an asset's
native size is NOT its on-screen size. Observed on-screen scales for these assets
ran 1.45 to 1.85 against our dpr-2 captures, not a uniform 2.0. So always pass
--calibrate with a frame that shows the element, and let this script find the
scale rather than assuming one.

USAGE
    .venv/bin/python engine/mint_template.py \\
        --asset ref/swf_assets/1746_54x53.png \\
        --name mission_start \\
        --calibrate ref/auto/mission/detail_02.png \\
        --negatives ref/auto/mission/room_05.png ref/auto/lobby_full.png

It refuses to write a template whose margin over the negatives is too thin,
because a template that cannot be separated from unrelated screens is worse than
no template — it produces confident wrong clicks.
"""
import argparse
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def opaque_core(img):
    """Largest fully-opaque axis-aligned box, grown from the alpha centroid.

    Grown from the centroid of the opaque pixels rather than the geometric
    centre, so an off-centre glyph still yields a usable core.
    """
    if img.ndim != 3 or img.shape[2] != 4:
        return img[:, :, :3] if img.ndim == 3 else img, False
    a = img[:, :, 3]
    op = (a > 250).astype(np.uint8)
    if op.min() == 1:
        return img[:, :, :3], False            # already fully opaque
    ys, xs = np.nonzero(op)
    if len(xs) == 0:
        raise SystemExit("asset is entirely transparent")
    cy, cx = int(ys.mean()), int(xs.mean())
    h, w = op.shape
    r = 0
    while True:
        y0, y1, x0, x1 = cy - (r + 1), cy + r + 2, cx - (r + 1), cx + r + 2
        if y0 < 0 or x0 < 0 or y1 > h or x1 > w or op[y0:y1, x0:x1].min() == 0:
            break
        r += 1
    if r < 4:
        return None, True                      # no usable core; caller should mask
    return img[cy - r:cy + r + 1, cx - r:cx + r + 1, :3], False


def best_scale(core, frame_gray, lo=1.0, hi=2.4, step=0.05):
    g = cv2.cvtColor(core, cv2.COLOR_BGR2GRAY)
    best = (-1.0, None, None)
    s = lo
    while s <= hi + 1e-9:
        h, w = int(g.shape[0] * s), int(g.shape[1] * s)
        if h >= 8 and w >= 8 and h <= frame_gray.shape[0] and w <= frame_gray.shape[1]:
            t = cv2.resize(g, (w, h), interpolation=cv2.INTER_LINEAR)
            r = cv2.matchTemplate(frame_gray, t, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(r)
            if mx > best[0]:
                best = (float(mx), round(s, 2), (loc[0] + w // 2, loc[1] + h // 2))
        s += step
    return best


def score(tpl_gray, path):
    f = cv2.imread(path)
    if f is None:
        return None
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    if tpl_gray.shape[0] > g.shape[0] or tpl_gray.shape[1] > g.shape[1]:
        return None
    r = cv2.matchTemplate(g, tpl_gray, cv2.TM_CCOEFF_NORMED)
    return float(cv2.minMaxLoc(r)[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", required=True)
    ap.add_argument("--name", required=True, help="template name -> tpl/<name>.png")
    ap.add_argument("--calibrate", required=True,
                    help="a frame that SHOWS the element, to find its on-screen scale")
    ap.add_argument("--negatives", nargs="*", default=[],
                    help="frames that do NOT show it, to measure separation")
    ap.add_argument("--min-margin", type=float, default=0.15)
    ap.add_argument("--min-conf", type=float, default=0.88)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    img = cv2.imread(a.asset, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit(f"cannot read {a.asset}")
    core, needs_mask = opaque_core(img)
    if core is None:
        raise SystemExit(
            "no fully-opaque core of usable size in this asset. Keep the alpha and "
            "let perceive.Template mask it instead — but calibrate its threshold "
            "separately, because masked matching uses TM_CCORR_NORMED and its "
            "baseline sits around 0.83, not 0.5.")
    print(f"asset {img.shape[1]}x{img.shape[0]}  ->  opaque core "
          f"{core.shape[1]}x{core.shape[0]}")

    cal = cv2.imread(a.calibrate)
    if cal is None:
        raise SystemExit(f"cannot read {a.calibrate}")
    conf, scale, at = best_scale(core, cv2.cvtColor(cal, cv2.COLOR_BGR2GRAY))
    print(f"calibrated on {os.path.basename(a.calibrate)}: "
          f"conf={conf:.3f} scale={scale} at={at}")

    out = cv2.resize(core, (int(core.shape[1] * scale), int(core.shape[0] * scale)),
                     interpolation=cv2.INTER_LINEAR)
    og = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)

    negs = []
    for p in a.negatives:
        v = score(og, p)
        if v is not None:
            negs.append((os.path.basename(p), v))
    worst = max((v for _, v in negs), default=0.0)
    print("negatives:")
    for n, v in sorted(negs, key=lambda t: -t[1]):
        print(f"    {n:32s} {v:.3f}")
    margin = conf - worst
    print(f"\npositive={conf:.3f}  worst negative={worst:.3f}  margin={margin:.3f}")

    if conf < a.min_conf and not a.force:
        raise SystemExit(f"REFUSING: positive {conf:.3f} < {a.min_conf}")
    if negs and margin < a.min_margin and not a.force:
        raise SystemExit(
            f"REFUSING: margin {margin:.3f} < {a.min_margin}. A template that "
            f"cannot be separated from unrelated screens produces confident wrong "
            f"clicks, which is worse than having no template at all.")

    dst = os.path.join(ROOT, "tpl", f"{a.name}.png")
    cv2.imwrite(dst, out)
    print(f"\nwrote {dst}  ({out.shape[1]}x{out.shape[0]}, opaque)")
    print(f"suggested threshold: {max(a.min_conf, round(worst + margin * 0.4, 2))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
