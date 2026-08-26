#!/usr/bin/env python3
"""Pull the bitmaps out of a Flash SWF.

Why this exists: templates cut from screenshots carry JPEG noise, whatever was
behind the element, and any hover state that happened to be active. The game's
own art has none of that. Feed the output to `engine/mint_template.py`.

WHERE THE SWFs COME FROM (reproducible)
---------------------------------------
    ninja_saga.exe                    Qt self-extracting installer, 100MB
      overlay @1,899,541              "IFSETUP_START" (bytes +1) then 7z
      7z payload                      filenames are base64(UTF-16LE)
      files/air.swf (21KB)            the whole AIR app: a CEF webview wrapper
        -> launcher.json              https://ninjasaga.cc/launcher.json
        -> CDN base                   https://cdn.ninjasaga.cc/cdn/swf/latest/
        -> ninja_saga.swf             the shell client (2.87MB, stage 960x720)
        -> 13 module paths in its ABC (swf/library/*, swf/panels/*, swf/actions/*)

So the module URL is CDN base + the path as written in the client's own
bytecode, e.g. .../cdn/swf/latest/swf/panels/mission_complete.swf. That double
"swf/" is not a mistake — the paths in the ABC are relative to a base that
already ends in swf/latest/.

WHAT IT DOES AND DOES NOT HANDLE
--------------------------------
Handles the bitmap tags: DefineBitsLossless/2 (zlib RGBA/RGB) and
DefineBitsJPEG2/3/4. Alpha is preserved, because it matters — see
mint_template.py for the measurement (masked 0.11 margin vs opaque-core 0.38).

Does NOT handle vector art (DefineShape*), which is most Flash UI. In the shell
client there are 421 DefineShape4 + 398 DefineShape + 237 DefineShape2 tags
against only 68 bitmaps, so a lot of the chrome is vectors and is simply not
reachable this way. For those, cut from a live capture instead.

Colour-mapped DefineBitsLossless (format 3) is skipped: it needs the palette
walked and none of the assets we wanted used it.

USAGE
    .venv/bin/python engine/swf_extract.py FILE.swf [FILE2.swf ...] --out DIR
"""
import argparse
import os
import struct
import sys
import zlib

import cv2
import numpy as np


def decompress(raw):
    """Return the SWF body after the 8-byte header, decompressed if needed."""
    sig = raw[:3]
    if sig == b'CWS':
        return zlib.decompress(raw[8:])
    if sig == b'ZWS':
        import lzma
        props = raw[12:17]
        dec = lzma.LZMADecompressor(
            format=lzma.FORMAT_RAW,
            filters=[{'id': lzma.FILTER_LZMA1,
                      'dict_size': struct.unpack('<I', props[1:5])[0]}])
        return dec.decompress(raw[17:])
    if sig == b'FWS':
        return raw[8:]
    raise ValueError(f"not a SWF (signature {sig!r})")


def stage_rect(body):
    """(width, height, fps, frames). The RECT is bit-packed twips."""
    p = 0
    nbits = body[0] >> 3
    bitpos = 5
    vals = []
    for _ in range(4):
        v = 0
        for _ in range(nbits):
            v = (v << 1) | ((body[bitpos >> 3] >> (7 - (bitpos & 7))) & 1)
            bitpos += 1
        vals.append(v)
    off = (bitpos + 7) // 8
    fps = struct.unpack_from('<H', body, off)[0] / 256.0
    frames = struct.unpack_from('<H', body, off + 2)[0]
    return ((vals[1] - vals[0]) // 20, (vals[3] - vals[2]) // 20, fps, frames, off + 4)


def extract(path, outdir, tag=None):
    raw = open(path, 'rb').read()
    body = decompress(raw)
    w, h, fps, frames, p = stage_rect(body)
    prefix = tag or os.path.splitext(os.path.basename(path))[0]
    os.makedirs(outdir, exist_ok=True)
    n = 0
    while p < len(body) - 1:
        th = struct.unpack_from('<H', body, p)[0]
        p += 2
        code, ln = th >> 6, th & 0x3F
        if ln == 0x3F:
            ln = struct.unpack_from('<I', body, p)[0]
            p += 4
        end = p + ln
        if code == 0:
            break
        try:
            img = None
            if code in (20, 36):                       # DefineBitsLossless / 2
                cid, fmt, bw, bh = struct.unpack_from('<HBHH', body, p)
                q = p + 7
                if fmt == 3:
                    q += 1                             # colour-mapped: skipped
                else:
                    data = zlib.decompress(body[q:end])
                    if fmt == 5:                       # 32-bit ARGB
                        a = np.frombuffer(data[:bw * bh * 4], np.uint8)
                        if a.size == bw * bh * 4:
                            a = a.reshape(bh, bw, 4)
                            img = a[:, :, [3, 2, 1, 0]]        # ARGB -> BGRA
                    elif fmt == 4:                     # 15-bit
                        a = np.frombuffer(data[:bw * bh * 2], '>u2')
                        if a.size == bw * bh:
                            a = a.reshape(bh, bw)
                            r = ((a >> 10) & 31) * 255 // 31
                            g = ((a >> 5) & 31) * 255 // 31
                            b = (a & 31) * 255 // 31
                            img = np.dstack([b, g, r]).astype(np.uint8)
            elif code in (21, 35, 90):                 # DefineBitsJPEG2 / 3 / 4
                cid = struct.unpack_from('<H', body, p)[0]
                q = p + 2
                if code in (35, 90):
                    alen = struct.unpack_from('<I', body, q)[0]
                    q += 4
                    if code == 90:
                        q += 2                         # deblock param
                    jpg = body[q:q + alen]
                    alpha_zip = body[q + alen:end]
                else:
                    jpg, alpha_zip = body[q:end], b''
                # Flash prefixes an erroneous EOI+SOI pair on some assets
                if jpg[:4] == b'\xff\xd9\xff\xd8':
                    jpg = jpg[4:]
                bgr = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                if bgr is not None:
                    img = bgr
                    if alpha_zip:
                        try:
                            al = zlib.decompress(alpha_zip)
                            need = bgr.shape[0] * bgr.shape[1]
                            if len(al) >= need:
                                a = np.frombuffer(al[:need], np.uint8).reshape(
                                    bgr.shape[0], bgr.shape[1])
                                img = np.dstack([bgr, a])
                        except Exception:
                            pass
            if img is not None and img.shape[0] >= 4 and img.shape[1] >= 4:
                fn = f"{prefix}_{cid:04d}_{img.shape[1]}x{img.shape[0]}.png"
                cv2.imwrite(os.path.join(outdir, fn), img)
                n += 1
        except Exception:
            pass
        p = end
    return {"file": os.path.basename(path), "stage": f"{w}x{h}", "fps": fps,
            "frames": frames, "bitmaps": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("swf", nargs="+")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    total = 0
    for f in a.swf:
        try:
            r = extract(f, a.out)
            print(f"  {r['bitmaps']:>4} bitmaps  stage={r['stage']:>9} "
                  f"fps={r['fps']:<5} frames={r['frames']:<4} {r['file']}")
            total += r['bitmaps']
        except Exception as e:
            print(f"  FAILED {os.path.basename(f)}: {type(e).__name__}: {e}")
    print(f"\n{total} bitmaps -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
