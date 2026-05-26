"""
Live audit of generate_signal() logic on BTCUSDT 1H candles.
Run: python audit_btc.py
"""
import sys
sys.path.insert(0, ".")

from mexc_api import MEXCSpotAPI
from strategy import (
    candles_to_df, detect_kill_zone, detect_market_structure,
    detect_premium_discount, detect_liquidity_sweep, detect_fvgs,
    detect_order_blocks, calculate_vwap, _is_displacement_candle,
    _is_confirmation_candle, _atr, generate_signal,
    FVG_MIN_SIZE_PCT,
)
from datetime import datetime, timezone
import config

api = MEXCSpotAPI()

klines_1h = api.get_klines("BTCUSDT", "60m", 100)
klines_4h = api.get_klines("BTCUSDT", "4h",  50)

df     = candles_to_df(klines_1h)
htf_df = candles_to_df(klines_4h)

current_price = float(df.iloc[-1]["close"])
n = len(df)

print("=" * 60)
print("BTCUSDT Live generate_signal() Audit")
print(f"UTC time    : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
print(f"Price       : {current_price:,.2f}")
print(f"1H candles  : {n}  |  4H candles: {len(htf_df)}")
print("=" * 60)

score = {
    "kill_zone": 0, "bos": 0, "discount": 0,
    "sweep": 0, "displacement": 0, "fvg": 0,
    "ob": 0, "ote": 0, "confirmation": 0, "vwap": 0,
}

# ── STEP 1: Kill Zone ─────────────────────────────────────────────────
kz = detect_kill_zone()
now = datetime.now(timezone.utc)
print(f"\n[1] Kill Zone")
print(f"    weekday={now.weekday()} (0=Mon)  hour={now.hour} UTC")
print(f"    result  = {kz}")
if kz is None:
    print("    STATUS  = FAIL  — outside kill zone hours (hard block)")
else:
    if kz != "FRIDAY_REDUCED":
        score["kill_zone"] = 1
    print(f"    STATUS  = PASS  (+{score['kill_zone']} pts)")

# ── STEP 2: Market Structure ──────────────────────────────────────────
ms = detect_market_structure(htf_df)
print(f"\n[2] Market Structure (4H)")
print(f"    bias         = {ms['bias']}")
print(f"    choch        = {ms['choch_detected']}")
print(f"    swing_high   = {ms['swing_high']:,.2f}")
print(f"    swing_low    = {ms['swing_low']:,.2f}")
print(f"    last_bos     = {ms['last_bos']:,.2f}")
if ms["choch_detected"]:
    print("    STATUS  = FAIL  — CHoCH detected (hard block)")
elif ms["bias"] == "BEARISH":
    print("    STATUS  = FAIL  — BEARISH structure (blocked even for daytrading)")
else:
    score["bos"] = 1
    print(f"    STATUS  = PASS  (+1 pts, bias={ms['bias']})")

# ── STEP 3: Premium / Discount ────────────────────────────────────────
pd_info = detect_premium_discount(df)
print(f"\n[3] Premium / Discount")
print(f"    zone         = {pd_info['zone']}")
print(f"    position_pct = {pd_info['position_pct']:.1f}%  (< 50=DISCOUNT, > 75=PREMIUM)")
print(f"    range_low    = {pd_info['range_low']:,.2f}")
print(f"    equilibrium  = {pd_info['equilibrium']:,.2f}")
print(f"    range_high   = {pd_info['range_high']:,.2f}")
print(f"    ote_zone     = {pd_info['ote_zone']}  (62–79 % Fib)")
if pd_info["zone"] == "PREMIUM":
    print("    STATUS  = FAIL  — price in PREMIUM (hard block)")
else:
    if pd_info["zone"] == "DISCOUNT":
        score["discount"] = 1
    if pd_info["ote_zone"]:
        score["ote"] = 1
    print(f"    STATUS  = PASS  (+{score['discount']} discount, +{score['ote']} ote)")

equilibrium = pd_info["equilibrium"]

# ── STEP 4: Liquidity Sweep ───────────────────────────────────────────
sweep = detect_liquidity_sweep(df)
print(f"\n[4] Liquidity Sweep (last 20 candles)")
print(f"    detected     = {sweep['sweep_detected']}")
print(f"    sweep_price  = {sweep['sweep_price']:,.2f}")
print(f"    strength     = {sweep['sweep_strength']}%")
if sweep["sweep_detected"]:
    score["sweep"] = 1
    print("    STATUS  = PASS  (+1 pts)")
else:
    print("    STATUS  = MISS  (no sweep, 0 pts — not a hard block)")

# ── STEP 5: Displacement Candle ───────────────────────────────────────
disp_found = False
disp_idx   = -1
print(f"\n[5] Displacement Candle (last 10 candles)")
for di in range(max(0, n - 10), n):
    if _is_displacement_candle(df, di):
        disp_found = True
        disp_idx = di
        c = df.iloc[di]
        body = float(c["close"]) - float(c["open"])
        rng  = float(c["high"])  - float(c["low"])
        body_pct = body / float(c["close"]) * 100
        start = max(0, di - 20)
        avg_vol = float(df.iloc[start:di]["volume"].mean()) if di > start else 0
        vol_ratio = float(c["volume"]) / avg_vol if avg_vol > 0 else 0
        print(f"    FOUND at index {di}: body={body_pct:.2f}%  vol_ratio={vol_ratio:.2f}x")
        score["displacement"] = 1
        break

if not disp_found:
    print("    Checking last 10 candles...")
    for di in range(max(0, n - 10), n):
        c = df.iloc[di]
        open_ = float(c["open"]); close_ = float(c["close"])
        high_ = float(c["high"]); low_   = float(c["low"])
        bull  = close_ > open_
        body  = close_ - open_ if bull else open_ - close_
        rng   = high_ - low_
        body_pct = body / close_ * 100 if close_ > 0 else 0
        wick_ratio = (high_ - close_) / rng if rng > 0 and bull else 999
        start = max(0, di - 20)
        avg_vol = float(df.iloc[start:di]["volume"].mean()) if di > start else 0
        vol_ratio = float(c["volume"]) / avg_vol if avg_vol > 0 else 0
        fails = []
        if not bull:         fails.append("bearish")
        if body_pct < 0.5:   fails.append(f"body too small ({body_pct:.3f}%<0.5%)")
        if wick_ratio > 0.2: fails.append(f"wick too large ({wick_ratio:.2f}>0.2)")
        if avg_vol > 0 and float(c["volume"]) < 1.5 * avg_vol:
            fails.append(f"low vol ({vol_ratio:.2f}x<1.5x)")
        print(f"    idx={di}: bull={bull} body={body_pct:.3f}% wick_ratio={wick_ratio:.2f} vol={vol_ratio:.2f}x  fails=[{', '.join(fails) if fails else 'none'}]")
    print("    STATUS  = MISS  (0 pts)")

# ── STEP 6 & 7: FVG + OB ─────────────────────────────────────────────
scan_window = min(n, config.OB_LOOKBACK * 2)
fvgs = detect_fvgs(df.iloc[-scan_window:].reset_index(drop=True))
obs  = detect_order_blocks(df.iloc[-scan_window:].reset_index(drop=True))

bull_fvgs     = [f for f in fvgs if f.type == "bullish"]
unfilled_fvgs = [f for f in bull_fvgs if not f.filled]
active_fvgs   = [f for f in unfilled_fvgs
                 if f.bottom <= current_price <= f.top and f.bottom < equilibrium]

bull_obs     = [o for o in obs if o.type == "bullish"]
unmitig_obs  = [o for o in bull_obs if not o.mitigated]
active_obs   = [o for o in unmitig_obs
                if o.bottom <= current_price <= o.top and o.bottom < equilibrium]

print(f"\n[6/7] FVG + OB  (scan_window={scan_window}, equilibrium={equilibrium:,.2f})")
print(f"    FVG_MIN_SIZE_PCT = {config.FVG_MIN_SIZE_PCT*100:.3f}%")
print(f"    OB_MIN_IMPULSE   = {config.OB_MIN_IMPULSE_PCT*100:.2f}%")
print(f"\n    All bullish FVGs: {len(bull_fvgs)}  |  unfilled: {len(unfilled_fvgs)}  |  active (in zone + discount): {len(active_fvgs)}")
for f in unfilled_fvgs:
    dist = (current_price - f.top) / current_price * 100 if current_price > f.top else (f.bottom - current_price) / current_price * 100 if current_price < f.bottom else 0
    in_discount = f.bottom < equilibrium
    inside = f.bottom <= current_price <= f.top
    print(f"      FVG [{f.bottom:,.2f} – {f.top:,.2f}]  in_discount={in_discount}  price_inside={inside}  dist_from_price={dist:.2f}%  formed_at={f.formed_at}")

print(f"\n    All bullish OBs : {len(bull_obs)}  |  unmitigated: {len(unmitig_obs)}  |  active (in zone + discount): {len(active_obs)}")
for o in unmitig_obs:
    dist = (current_price - o.top) / current_price * 100 if current_price > o.top else (o.bottom - current_price) / current_price * 100 if current_price < o.bottom else 0
    in_discount = o.bottom < equilibrium
    inside = o.bottom <= current_price <= o.top
    print(f"      OB  [{o.bottom:,.2f} – {o.top:,.2f}]  in_discount={in_discount}  price_inside={inside}  dist_from_price={dist:.2f}%  formed_at={o.formed_at}")

if not active_fvgs and not active_obs:
    print("    STATUS  = FAIL  — no active FVG or OB (hard block)")
else:
    if active_fvgs: score["fvg"] = 1
    if active_obs and active_fvgs: score["ob"] = 1
    print(f"    STATUS  = PASS  (+{score['fvg']} fvg, +{score['ob']} ob)")

# ── STEP 8: VWAP ──────────────────────────────────────────────────────
vwap = calculate_vwap(df)
print(f"\n[8] VWAP")
print(f"    vwap  = {vwap:,.2f}")
pass_fail = "PASS" if current_price <= vwap else "FAIL"
print(f"    price = {current_price:,.2f}  ({pass_fail})")
if vwap > 0 and current_price <= vwap:
    score["vwap"] = 1
    print("    STATUS  = PASS  (+1 pts)")
else:
    print("    STATUS  = MISS  (0 pts)")

# ── STEP 9: Confirmation candle ───────────────────────────────────────
conf = _is_confirmation_candle(df, fvgs, obs)
print(f"\n[9] Confirmation Candle")
last = df.iloc[-1]
print(f"    last candle: open={float(last['open']):,.2f} close={float(last['close']):,.2f} bull={float(last['close'])>float(last['open'])}")
print(f"    in FVG/OB  = {conf}")
if conf:
    score["confirmation"] = 1
    print("    STATUS  = PASS  (+1 pts)")
else:
    print("    STATUS  = MISS  (0 pts)")

# ── SCORE SUMMARY ─────────────────────────────────────────────────────
total = sum(score.values())
print(f"\n{'='*60}")
print(f"SCORE SUMMARY: {total}/10  (minimum required: 8)")
print(f"Breakdown: {score}")
print(f"{'='*60}")

if total >= 8:
    print("RESULT: SIGNAL WOULD BE GENERATED")
else:
    missing = 8 - total
    zero_keys = [k for k, v in score.items() if v == 0]
    print(f"RESULT: NO SIGNAL — need {missing} more point(s)")
    print(f"Missing points from: {zero_keys}")

# ── Also call generate_signal() directly ─────────────────────────────
print(f"\n{'='*60}")
print("Direct generate_signal() call (trade_type=daytrading):")
import logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
sig = generate_signal("BTCUSDT", df, htf_df=htf_df, trade_type="daytrading")
if sig:
    print(f"SIGNAL: score={sig.score} zone={sig.zone_type} entry={sig.entry_price:.2f} sl={sig.stop_loss:.2f} tp1={sig.tp1:.2f}")
else:
    print("SIGNAL: None")
