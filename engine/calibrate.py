"""Threshold calibration against real captured frames.

Templates were cut at native canvas resolution (host dpr 2). The saved reference
frames are MCP screenshots at 0.9074x CSS, and native = 2x CSS, so a template
should match its source frame at scale 0.9074/2 = 0.4537. We scan a range instead
of assuming, because the location of the peak tells us the true scale and the
sharpness around it tells us whether multi-scale matching is required at runtime.
"""
import sys, cv2, numpy as np, pathlib

# template -> the reference frame it was cut from
SRC = {
 'claim_daily':            'state_home_daily_reward_popup.jpg',
 'close_popup_x':          'state_home_daily_reward_popup.jpg',
 'day_claimed_check':      'state_home_daily_reward_popup.jpg',
 'day_current_pointer':    'state_home_daily_reward_popup.jpg',
 'close_popup_x_large':    'state_daily_calendar.jpg',
 'close_popup_back_arrow': 'state_wishing_tree.jpg',
 'wish_btn':               'state_wishing_tree.jpg',
 'spin_btn':               'state_daily_lucky_spin.jpg',
 'lobby_logo':             'state_lobby_village.jpg',
 'nav_profile':            'state_lobby_village.jpg',
 'nav_jutsu':              'state_lobby_village.jpg',
 'nav_option':             'state_lobby_village.jpg',
 'invite_reward_btn':      'state_lobby_village.jpg',
 'hunting_house_btn':      'state_hunting_submenu.jpg',
 'close_popup_x_menu':     'state_hunting_submenu.jpg',
 'loading_text':           'state_loading_stalled.jpg',
}

def gray(p, unchanged=False):
    img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED if unchanged else cv2.IMREAD_COLOR)
    if img is None: return None
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img

def best_at(frame, tpl, s):
    h, w = tpl.shape[:2]
    t = cv2.resize(tpl, (max(1,int(w*s)), max(1,int(h*s))),
                   interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    if t.shape[0] > frame.shape[0] or t.shape[1] > frame.shape[1]:
        return None, None
    res = cv2.matchTemplate(frame, t, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    return float(mx), (loc[0] + t.shape[1]//2, loc[1] + t.shape[0]//2)

rows = []
for name, ref in sorted(SRC.items()):
    tp, rp = pathlib.Path('tpl')/f'{name}.png', pathlib.Path('ref/raw')/ref
    tpl, frame = gray(tp, True), gray(rp)
    if tpl is None or frame is None:
        rows.append((name, None, None, None, None, 'MISSING FILE')); continue
    # coarse then fine scan for the peak
    coarse = [(s/1000.0) for s in range(300, 701, 10)]
    peak_s, peak_c = max(((s, best_at(frame, tpl, s)[0]) for s in coarse),
                         key=lambda kv: (kv[1] is not None, kv[1] or -1))
    fine = [max(0.05, peak_s + d/1000.0) for d in range(-12, 13, 2)]
    peak_s, peak_c = max(((s, best_at(frame, tpl, s)[0]) for s in fine),
                         key=lambda kv: (kv[1] is not None, kv[1] or -1))
    _, at = best_at(frame, tpl, peak_s)
    # sensitivity: how much confidence is lost 8% off the peak scale
    off = [best_at(frame, tpl, peak_s*k)[0] for k in (0.92, 1.08)]
    off = [o for o in off if o is not None]
    drop = (peak_c - max(off)) if off else float('nan')
    rows.append((name, peak_c, peak_s, at, drop, ''))

print(f"{'template':24s} {'peak conf':>9s} {'@scale':>7s} {'drop@±8%':>9s}  location")
print('-'*76)
for n, c, s, at, d, err in rows:
    if err: print(f"{n:24s} {'--':>9s} {'--':>7s} {'--':>9s}  {err}"); continue
    print(f"{n:24s} {c:9.4f} {s:7.3f} {d:9.4f}  {at}")

good = [r for r in rows if r[1] is not None]
if good:
    cs = [r[1] for r in good]; ss = [r[2] for r in good]
    print('-'*76)
    print(f"n={len(good)}  conf min={min(cs):.4f} median={sorted(cs)[len(cs)//2]:.4f} max={max(cs):.4f}")
    print(f"scale peaks: min={min(ss):.3f} max={max(ss):.3f}  (predicted 0.454)")
