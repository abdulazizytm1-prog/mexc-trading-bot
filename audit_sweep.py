"""
Post-fix live test: sweep + displacement across 4 coins.
Run: python audit_sweep.py
"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.WARNING)

from mexc_api import MEXCSpotAPI
from strategy import (
    candles_to_df, detect_kill_zone, detect_market_structure,
    detect_premium_discount, detect_liquidity_sweep, detect_fvgs,
    detect_order_blocks, calculate_vwap,
    _is_displacement_candle, _is_two_candle_displacement,
    generate_signal,
)
from datetime import datetime, timezone
import config

api    = MEXCSpotAPI()
COINS  = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "TRXUSDT"]
now    = datetime.now(timezone.utc)
kz     = detect_kill_zone()

print("=" * 65)
print(f"Sweep + Displacement fix — live test  {now.strftime('%Y-%m-%d %H:%M UTC')}")
print(f"Kill zone : {kz or 'NONE (outside hours)'}")
print("=" * 65)

for symbol in COINS:
    print(f"\n{'─'*65}")
    print(f"  {symbol}")
    print(f"{'─'*65}")

    try:
        klines_1h = api.get_klines(symbol, "60m", 100)
        klines_4h = api.get_klines(symbol, "4h",  50)
        df     = candles_to_df(klines_1h)
        htf_df = candles_to_df(klines_4h)
        n      = len(df)
        price  = float(df.iloc[-1]["close"])
    except Exception as e:
        print(f"  ERROR fetching candles: {e}")
        continue

    print(f"  Price  : {price:,.4f}")

    # ── Sweep ──────────────────────────────────────────────────────────
    sweep = detect_liquidity_sweep(df)
    sw_str = (
        f"YES  level={sweep['sweep_price']:,.4f}  "
        f"strength={sweep['sweep_strength']:.3f}%  "
        f"candle_idx={sweep['sweep_candle_index']}"
        if sweep["sweep_detected"] else "NO"
    )
    print(f"  Sweep  : {sw_str}")

    # ── Displacement ───────────────────────────────────────────────────
    disp_single = False
    disp_two    = False
    disp_idx    = -1
    for di in range(max(0, n - 10), n):
        if _is_displacement_candle(df, di):
            disp_single = True
            disp_idx    = di
            break
        if _is_two_candle_displacement(df, di):
            disp_two = True
            disp_idx = di
            break

    if disp_single or disp_two:
        c  = df.iloc[disp_idx]
        if disp_single:
            body_pct = (float(c["close"]) - float(c["open"])) / float(c["close"]) * 100
            start    = max(0, disp_idx - 20)
            avg_v    = float(df.iloc[start:disp_idx]["volume"].mean()) if disp_idx > start else 1
            vratio   = float(c["volume"]) / avg_v if avg_v > 0 else 0
            kind     = f"single  body={body_pct:.3f}%  vol={vratio:.2f}x  idx={disp_idx}"
        else:
            c1   = df.iloc[disp_idx - 1]
            comb = (float(c["close"]) - float(c1["open"])) / float(c["close"]) * 100
            kind = f"2-candle  combined={comb:.3f}%  idx={disp_idx-1}+{disp_idx}"
        print(f"  Displ  : YES  {kind}")
    else:
        # Explain best candidate
        best_body = 0.0
        for di in range(max(0, n - 10), n):
            c = df.iloc[di]
            o, cl = float(c["open"]), float(c["close"])
            if cl > o:
                bp = (cl - o) / cl * 100
                best_body = max(best_body, bp)
        print(f"  Displ  : NO   (best bullish body={best_body:.3f}% in last 10)")

    # ── Full generate_signal() ─────────────────────────────────────────
    pd_info = detect_premium_discount(df)
    ms      = detect_market_structure(htf_df)
    vwap    = calculate_vwap(df)
    eq      = pd_info["equilibrium"]

    scan_window  = min(n, config.OB_LOOKBACK * 2)
    fvgs = detect_fvgs(df.iloc[-scan_window:].reset_index(drop=True))
    obs  = detect_order_blocks(df.iloc[-scan_window:].reset_index(drop=True))
    active_fvgs = [
        f for f in fvgs if f.type == "bullish" and not f.filled
        and price <= f.top * 1.005 and f.bottom < price * 1.005 and f.bottom < eq
    ]
    active_obs  = [
        o for o in obs  if o.type == "bullish" and not o.mitigated
        and price <= o.top * 1.005 and o.bottom < price * 1.005 and o.bottom < eq
    ]

    print(f"  Zone   : {pd_info['zone']} ({pd_info['position_pct']:.1f}%)  "
          f"OTE={pd_info['ote_zone']}  4H={ms['bias']}  VWAP={'OK' if price <= vwap else 'ABOVE'}")
    print(f"  FVGs   : {len([f for f in fvgs if f.type=='bullish' and not f.filled])} unfilled  "
          f"active={len(active_fvgs)}")
    print(f"  OBs    : {len([o for o in obs if o.type=='bullish' and not o.mitigated])} unmitigated  "
          f"active={len(active_obs)}")

    sig = generate_signal(symbol, df, htf_df=htf_df, trade_type="daytrading")
    if sig:
        print(f"  SIGNAL : score={sig.score}/10  zone={sig.zone_type}  "
              f"entry={sig.entry_price:,.4f}  sl={sig.stop_loss:,.4f}  tp1={sig.tp1:,.4f}")
        print(f"           reason={sig.reason}")
    else:
        # Manually compute score to show partial result
        score = 0
        reasons = []
        if kz:
            score += 1; reasons.append("kill_zone")
        if ms["bias"] != "BEARISH" and not ms["choch_detected"]:
            score += 1; reasons.append("bos")
        if pd_info["zone"] != "PREMIUM":
            if pd_info["zone"] == "DISCOUNT":
                score += 1; reasons.append("discount")
            if pd_info["ote_zone"]:
                score += 1; reasons.append("ote")
        if sweep["sweep_detected"]:
            score += 1; reasons.append("sweep")
        if disp_single or disp_two:
            score += 1; reasons.append("displacement")
        if active_fvgs:
            score += 1; reasons.append("fvg")
        if active_obs:
            score += 1; reasons.append("ob")
        if price <= vwap:
            score += 1; reasons.append("vwap")
        blocker = ""
        if pd_info["zone"] == "PREMIUM":     blocker = "PREMIUM zone"
        elif ms["choch_detected"]:           blocker = "CHoCH"
        elif ms["bias"] == "BEARISH":        blocker = "BEARISH structure"
        elif kz is None:                     blocker = "outside kill zone"
        elif not active_fvgs and not active_obs: blocker = "no active FVG/OB"
        elif score < 6:                      blocker = f"score {score} < 6"
        print(f"  SIGNAL : None  (score={score}/10  pts=[{', '.join(reasons)}]"
              f"  blocker={blocker})")

print(f"\n{'='*65}")
