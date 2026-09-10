#!/usr/bin/env python3
"""Build the hand-seal catalogue from harvested crops — offline, once.

WHY THIS EXISTS
---------------
`seals.load_catalogue()` reads `ref/auto/tp/seal_catalogue/`, and that
directory HAD NEVER BEEN CREATED. There was no builder. So the catalogue was
always empty, `identify()` always returned None, and every round fell through
to cross-rendering matching - the weak direction the code itself warns about.

That is not a cosmetic gap. Measured over one live mission, with the board's
own HUD as the verdict:

    round   thinnest margin   board
      1           9.32x       advanced
      2         * 1.01x       HEART LOST
      3           5.78x       advanced
      4           3.85x       advanced
      5         * 1.06x       HEART LOST
      6           2.63x       (board closed)

Both failures were ~1.0x margins; everything at 2.6x or better was accepted.
The bot memorises the sequence perfectly every time - what fails is mapping a
seal drawn SMALL ON A SLOT CARD over animated flames to the same seal drawn
LARGE IN A WOODEN FRAME on a tile. Two of the ten have near-identical blue
glove silhouettes, and on both failures the ink tiebreak was consulted and
AGREED WITH THE WRONG ANSWER.

THE METHOD, which is the one `load_catalogue`'s docstring already specifies
-----------------------------------------------------------------------------
Comparing a drawing to the SAME KIND of drawing is the strong direction, so
identity is established WITHIN each rendering first and the two are linked
once, here, offline:

  1. split the harvest by CROP SIZE, not by filename - 104x104 is tile art and
     140x170 is slot art. Sizes are a property of the thing; filenames were
     written by several different code paths and are not uniform.
  2. cluster within each rendering by single linkage on the blue-glove shape
     signature. Both distance distributions are cleanly bimodal WITH AN EMPTY
     GAP, which is what makes the threshold a measurement rather than a guess:

         tile pairs   0.000..0.104   then NOTHING until 0.156
         slot pairs   0.000..0.051   then a gap, a small bump, another gap

  3. link slot clusters to tile clusters by a one-to-one assignment on mean
     cross-distance. One-to-one is the point: the single genuinely ambiguous
     pair is settled BY ELIMINATION rather than by a coin flip, which is
     exactly what the live matcher cannot do because it decides each sign
     independently.

An unlinked seal is simply left out. A partial catalogue is still strictly
better than none: `identify()` returns None for anything it does not know and
the caller falls back to the old path, so the seals that ARE catalogued stop
being guessed at.

    python engine/build_seal_catalogue.py            # report only
    python engine/build_seal_catalogue.py --write    # write the catalogue
"""
import argparse
import glob
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import seals  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARVEST = os.path.join(ROOT, "ref/auto/tp/seals")
OUT = os.path.join(ROOT, "ref/auto/tp/seal_catalogue")

# Crop sizes identify the rendering. Measured across the whole harvest:
# 104x104 appears 196 times and 140x170 appears 110 times, and nothing else.
TILE_SIZE = (104, 104)
SLOT_SIZE = (140, 170)

# Thresholds sit inside the EMPTY GAP of each distribution, and both are flat
# over a wide range - tile gives 10 clusters for every threshold from 0.05 to
# 0.15, slot gives 9 from 0.03 to 0.12 - so neither is balanced on a knife edge.
TILE_THR = 0.10
SLOT_THR = 0.08

# How many exemplars of each rendering to keep per seal. More is not better:
# `identify` takes the MINIMUM distance over a seal's exemplars, so a single
# mislabelled crop can only ever pull a seal's score DOWN toward a wrong
# match. Keeping the medoids - the most typical crops - bounds that risk.
PER_SEAL = 4


def _load():
    """(tiles, slots), each a list of (path, signature, image)."""
    tiles, slots = [], []
    for p in sorted(glob.glob(os.path.join(HARVEST, "*.png"))):
        im = cv2.imread(p)
        if im is None:
            continue
        sig = seals._shape(seals.blue_mask(im))
        if sig is None:
            continue                        # no glove found; nothing to match on
        wh = (im.shape[1], im.shape[0])
        if wh == TILE_SIZE:
            tiles.append((p, sig, im))
        elif wh == SLOT_SIZE:
            slots.append((p, sig, im))
    return tiles, slots


def _pairwise(items):
    n = len(items)
    D = np.zeros((n, n), np.float32)
    for i in range(n):
        for j in range(i + 1, n):
            D[i, j] = D[j, i] = seals.dist(items[i][1], items[j][1])
    return D


def _cluster(D, thr):
    """Single-linkage clusters as index lists.

    A PROPER union-find. The first attempt used path halving written as
    `lab[a] = lab[lab[a]]; a = lab[a]`, which does not settle on a single root,
    and the giveaway was that the cluster sizes SUMMED TO MORE THAN THE INPUT -
    220 members from 208 crops. A partition whose parts do not sum to the whole
    is not a partition, and the count it reports cannot be trusted either.
    """
    n = D.shape[0]
    parent = list(range(n))

    def find(a):
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != root:          # compress, without losing the root
            parent[a], a = root, parent[a]
        return root

    for i in range(n):
        for j in range(i + 1, n):
            if D[i, j] <= thr:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[ra] = rb
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = sorted(groups.values(), key=len, reverse=True)
    assert sum(len(g) for g in out) == n, "clusters must partition the input"
    return out


def _medoids(idx, D, k):
    """The k most typical members of a cluster - smallest total distance."""
    if len(idx) <= k:
        return list(idx)
    tot = [(float(D[i, idx].sum()), i) for i in idx]
    tot.sort()
    return [i for _, i in tot[:k]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="write ref/auto/tp/seal_catalogue/ (replaces it)")
    a = ap.parse_args()

    tiles, slots = _load()
    print(f"harvest: {len(tiles)} tile crop(s), {len(slots)} slot crop(s)")
    if not tiles or not slots:
        sys.exit("nothing to build from - run a hand-seal round with "
                 "--save-crops first")

    Dt, Ds = _pairwise(tiles), _pairwise(slots)
    ct, cs = _cluster(Dt, TILE_THR), _cluster(Ds, SLOT_THR)
    print(f"  tile clusters @ {TILE_THR}: {len(ct)}  sizes {[len(g) for g in ct]}")
    print(f"  slot clusters @ {SLOT_THR}: {len(cs)}  sizes {[len(g) for g in cs]}")
    if len(ct) != seals.N_TILES:
        print(f"  NOTE: expected {seals.N_TILES} tile identities, got {len(ct)}. "
              f"The catalogue is built anyway for what IS separable.")

    # --- LINK THE TWO RENDERINGS, one-to-one ----------------------------
    #
    # Mean cross-distance between every slot cluster and every tile cluster,
    # then a greedy assignment over the whole matrix taking the most confident
    # pair first. Greedy on a globally sorted list is not optimal in general,
    # but it is the elimination argument the live matcher lacks: once a tile
    # identity is spoken for it cannot be offered to another sign.
    M = np.zeros((len(cs), len(ct)), np.float32)
    for i, sg in enumerate(cs):
        for j, tg in enumerate(ct):
            M[i, j] = float(np.mean([seals.dist(slots[si][1], tiles[ti][1])
                                     for si in sg for ti in tg]))
    order = sorted(((M[i, j], i, j) for i in range(len(cs))
                    for j in range(len(ct))))
    used_s, used_t, pairs = set(), set(), []
    for d, i, j in order:
        if i in used_s or j in used_t:
            continue
        used_s.add(i)
        used_t.add(j)
        pairs.append((d, i, j))
    pairs.sort()

    print("\n  linked slot cluster -> tile cluster (best first):")
    print(f"    {'seal':>5}{'mean d':>9}{'runner-up':>11}{'margin':>9}"
          f"   slots  tiles")
    for sid, (d, i, j) in enumerate(pairs):
        # the next-best tile still available to this slot cluster, for margin
        alts = sorted(M[i, k] for k in range(len(ct)) if k != j)
        second = alts[0] if alts else 1e9
        print(f"    {sid:>5}{d:>9.3f}{second:>11.3f}"
              f"{second / max(1e-6, d):>8.2f}x   {len(cs[i]):>5}  {len(ct[j]):>5}")

    if not a.write:
        print("\n  (report only - pass --write to create the catalogue)")
        return 0

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    n = 0
    for sid, (_d, i, j) in enumerate(pairs):
        for k, si in enumerate(_medoids(cs[i], Ds, PER_SEAL)):
            cv2.imwrite(os.path.join(OUT, f"seal{sid:02d}_slot{k}.png"),
                        slots[si][2])
            n += 1
        for k, ti in enumerate(_medoids(ct[j], Dt, PER_SEAL)):
            cv2.imwrite(os.path.join(OUT, f"seal{sid:02d}_tile{k}.png"),
                        tiles[ti][2])
            n += 1
    print(f"\n  wrote {n} exemplar(s) for {len(pairs)} seal(s) -> "
          f"{os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
