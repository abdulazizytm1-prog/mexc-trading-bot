"""Live re-test after all 6 fixes — run: python audit_btc2.py"""
import sys
sys.path.insert(0, ".")

from mexc_api import MEXCSpotAPI
from strategy import (
    candles_to_df, detect_kill_zone, detect_market_structure,
    detect_premium_discount, detect_liquidity_sweep, detect_fvgs,
    detect_order_blocks, calculate_vwap, _is_displacement_candle,
    _is_confirmation_candle, generate_signal,
)
from datetime import datetime, timezone
import config, logging

logging.basicConfig(level=logging.WARNING)   # suppress library noise

api = MEXCSpotAPI()
klines_1h = api.get_klines("BTCUSDT", "60m", 100)
klines_4h = api.get_klines("BTCUSDT", "4h",  50)
df      = candles_to_df(klines_1h)
htf_df  = candles_to_df(klines_4h)
n = len(df)

current_price = float(df.iloc[-1]["close"])
now = datetime.now(timezone.utc)

print("=" * 62)
print("BTCUSDT  Post-Fix Audit  (all 6 solutions applied)")
print(f"UTC      : {now.strftime('%Y-%m-%d %H:%M')}")
print(f"Price    : {current_price:,.2f}")
print("=" * 62)

score = {
    "kill_zone": 0, "bos": 0, "discount": 0,
    "sweep": 0, "displacement": 0, "fvg": 0,
    "ob": 0, "ote": 0, "confirmation": 0, "vwap": 0,
}

# ── 1. Kill Zone ──────────────────────────────────────────────────────
kz = detect_kill_zone()
if kz is not None and kz != "FRIDAY_REDUCED":
    score["kill_zone"] = 1
kz_str = kz or "NONE (outside hours)"
print(f"\n[1] Kill Zone     : {kz_str}  -> {'+1' if score['kill_zone'] else '0'} pts")

# ── 2. Market Structure ───────────────────────────────────────────────
ms = detect_market_structure(htf_df)
choch  = ms["choch_detected"]
bias   = ms["bias"]
if not choch and bias != "BEARISH":
    score["bos"] = 1
print(f"[2] Structure 4H  : {bias}  choch={choch}  -> {'+1' if score['bos'] else '0 BLOCKED'} pts")

# ── 3. Premium / Discount + OTE ───────────────────────────────────────
pd_info     = detect_premium_discount(df)
zone        = pd_info["zone"]
pct         = pd_info["position_pct"]
equilibrium = pd_info["equilibrium"]
if zone == "DISCOUNT":
    score["discount"] = 1
if pd_info["ote_zone"]:
    score["ote"] = 1
print(f"[3] Zone          : {zone} ({pct:.1f}%)  eq={equilibrium:,.0f}"
      f"  ote={pd_info['ote_zone']}  -> +{score['discount']} disc +{score['ote']} ote")
if zone == "PREMIUM":
    print("    *** PREMIUM = hard block ***")

# ── 4. Liquidity Sweep ────────────────────────────────────────────────
sweep = detect_liquidity_sweep(df)
if sweep["sweep_detected"]:
    score["sweep"] = 1
print(f"[4] Sweep         : {sweep['sweep_detected']}  -> {'+1' if score['sweep'] else '0'} pts")

# ── 5. Displacement ───────────────────────────────────────────────────
disp_found = False
for di in range(max(0, n - 10), n):
    if _is_displacement_candle(df, di):
        disp_found = True
        c = df.iloc[di]
        body_pct = (float(c["close"]) - float(c["open"])) / float(c["close"]) * 100
        start = max(0, di - 20)
        avg_v = float(df.iloc[start:di]["volume"].mean()) if di > start else 1.0
        vratio = float(c["volume"]) / avg_v if avg_v > 0 else 0
        score["displacement"] = 1
        print(f"[5] Displacement  : FOUND idx={di} body={body_pct:.3f}% vol={vratio:.2f}x  -> +1 pts")
        break
if not disp_found:
    # Show why each candidate failed
    best = ""
    best_body = 0.0
    for di in range(max(0, n - 10), n):
        c = df.iloc[di]
        o, cl = float(c["open"]), float(c["close"])
        if cl <= o:
            continue
        b = (cl - o) / cl * 100
        if b > best_body:
            best_body = b
            best = f"body={b:.3f}%"
    print(f"[5] Displacement  : NOT FOUND  (best bullish body={best_body:.3f}%)  -> 0 pts")

# ── 6/7. FVG + OB ─────────────────────────────────────────────────────
scan_window = min(n, config.OB_LOOKBACK * 2)
fvgs = detect_fvgs(df.iloc[-scan_window:].reset_index(drop=True))
obs  = detect_order_blocks(df.iloc[-scan_window:].reset_index(drop=True))

# Unfilled counts (after fix — uses close, not wick)
unfilled_fvgs = [f for f in fvgs if f.type == "bullish" and not f.filled]
unmitig_obs   = [o for o in obs  if o.type == "bullish" and not o.mitigated]

# Active (with new 0.5%-approach logic)
active_fvgs = [
    f for f in unfilled_fvgs
    if current_price <= f.top * 1.005
    and f.bottom < current_price * 1.005
    and f.bottom < equilibrium
]
active_obs = [
    o for o in unmitig_obs
    if current_price <= o.top * 1.005
    and o.bottom < current_price * 1.005
    and o.bottom < equilibrium
]

if active_fvgs:
    score["fvg"] = 1
if active_obs:
    score["ob"] = 1

print(f"[6] FVGs          : {len(unfilled_fvgs)} unfilled (was 0 before fix)  active={len(active_fvgs)}  -> {'+1' if score['fvg'] else '0'} pts")
for f in unfilled_fvgs:
    dist = (current_price - f.top) / current_price * 100 if current_price > f.top else 0
    inside = current_price <= f.top * 1.005 and f.bottom < current_price * 1.005
    print(f"     FVG [{f.bottom:,.0f}-{f.top:,.0f}]  dist_above={dist:.2f}%  active={inside}  discount={f.bottom < equilibrium}")

print(f"[7] OBs           : {len(unmitig_obs)} unmitigated  active={len(active_obs)}  -> {'+1' if score['ob'] else '0'} pts  (independent scoring)")
for o in unmitig_obs:
    dist = (current_price - o.top) / current_price * 100 if current_price > o.top else 0
    inside = current_price <= o.top * 1.005 and o.bottom < current_price * 1.005
    print(f"     OB  [{o.bottom:,.0f}-{o.top:,.0f}]  dist_above={dist:.2f}%  active={inside}  discount={o.bottom < equilibrium}")

if not active_fvgs and not active_obs:
    print("     *** NO ACTIVE ZONE = hard block ***")

# ── 8. VWAP ───────────────────────────────────────────────────────────
vwap = calculate_vwap(df)
if vwap > 0 and current_price <= vwap:
    score["vwap"] = 1
print(f"[8] VWAP          : price={current_price:,.0f}  vwap={vwap:,.0f}  below={'YES' if score['vwap'] else 'NO'}  -> {'+1' if score['vwap'] else '0'} pts")

# ── 9. Confirmation ───────────────────────────────────────────────────
conf = _is_confirmation_candle(df, fvgs, obs)
if conf:
    score["confirmation"] = 1
print(f"[9] Confirm candle: {conf}  -> {'+1' if conf else '0'} pts")

# ── Score summary ─────────────────────────────────────────────────────
total = sum(score.values())
print(f"\n{'=' * 62}")
print(f"SCORE  : {total}/10  (min required now: 6)")
print(f"BREAKDOWN: {score}")
missed = [k for k, v in score.items() if v == 0]
print(f"Missed : {missed}")
print(f"{'=' * 62}")

if zone == "PREMIUM":
    print("RESULT : NO SIGNAL (PREMIUM hard block)")
elif ms["choch_detected"]:
    print("RESULT : NO SIGNAL (CHoCH hard block)")
elif kz is None:
    print("RESULT : NO SIGNAL (outside kill zone)")
elif not active_fvgs and not active_obs:
    print("RESULT : NO SIGNAL (no active FVG or OB)")
elif total < 6:
    print(f"RESULT : NO SIGNAL (score {total} < 6)")
else:
    print(f"RESULT : SIGNAL CANDIDATE  (score {total}/10 passes pre-Claude gate)")

# ── Direct generate_signal() call ─────────────────────────────────────
print(f"\n{'=' * 62}")
print("generate_signal() direct call:")
logging.getLogger().setLevel(logging.INFO)
sig = generate_signal("BTCUSDT", df, htf_df=htf_df, trade_type="daytrading")
if sig:
    print(f"  score={sig.score}  zone={sig.zone_type}  entry={sig.entry_price:,.2f}"
          f"  sl={sig.stop_loss:,.2f}  tp1={sig.tp1:,.2f}  reason={sig.reason}")
else:
    print("  -> None (no signal generated)")
