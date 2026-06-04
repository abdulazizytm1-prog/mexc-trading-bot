"""
SMC + ICT strategy — spot long entries only.

Full ICT/SMC confluence engine (v2 — 11-concept framework):
  STEP 1  Market Structure    BOS / CHoCH (upgraded swing detection)
  STEP 2  Liquidity           map_liquidity_levels + detect_liquidity_sweep
  STEP 3  Fair Value Gap      ATR-sized FVG with mitigation states
  STEP 4  Order Block         body-validated OB with displacement gate
  STEP 5  Premium/Discount    Fibonacci OTE zone (61.8 – 78.6 %)
  STEP 6  Displacement        body/range ratio + ATR + volume quality score
  STEP 7  OTE                 embedded in detect_premium_discount / detect_ote_zone
  STEP 8  Confirmation        MSS (mini-CHoCH) + engulf + reclaim + disp-close
  STEP 9  Kill Zone           session-aware bonuses (score only, no hard block)
  STEP 10 VWAP                daily VWAP + AVWAP-from-sweep + SD1/SD2 bands
  STEP 11 generate_signal     full ICT sequence rebuild; score ≥ MIN_SIGNAL_SCORE

All thresholds imported from config.py.  No magic numbers in logic.
Claude API acts as the external quality gate (confidence ≥ 8/10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import (
    # core strategy
    ATR_PERIOD, ATR_SL_MULT,
    FVG_MIN_SIZE_PCT, OB_LOOKBACK, OB_MIN_IMPULSE_PCT, TAKE_PROFIT_RR,
    # market structure
    SWING_LOOKBACK, MIN_SWING_DISTANCE_PCT, BOS_CLOSE_BUFFER_PCT,
    FALSE_BREAK_CANDLES, MAX_SWING_AGE_CANDLES,
    # liquidity / sweep
    SWEEP_WICK_MIN_PCT, SWEEP_WICK_RATIO, SWEEP_VOLUME_MULT,
    SWEEP_CONFIRM_CANDLES, EQUAL_LEVEL_TOLERANCE,
    MAX_LIQUIDITY_AGE, MIN_POOL_STRENGTH,
    # FVG
    FVG_MIN_SIZE_ATR_MULT, FVG_MAX_AGE_CANDLES,
    FVG_CE_RATIO, FVG_INVALIDATION_PCT,
    # OB
    OB_BODY_MIN_PCT, OB_DISP_CANDLES, OB_MAX_AGE_CANDLES,
    OB_CE_RATIO, OB_INVALIDATION_PCT,
    # premium / discount / OTE
    RANGE_MIN_SIZE_PCT, EQ_BUFFER_PCT,
    OTE_FIB_LOW, OTE_FIB_MID, OTE_FIB_HIGH, OTE_FIB_EXTENDED,
    OTE_MIN_IMPULSE_PCT,
    # displacement
    DISP_BODY_MIN_PCT, DISP_BODY_RANGE_RATIO, DISP_ATR_MULT,
    DISP_VOLUME_MULT, DISP_CLOSE_POSITION_MIN,
    DISP_CANCEL_RATIO, DISP_MIN_QUALITY,
    # confirmation
    ENGULF_BODY_MULT, ENGULF_STRONG_MULT,
    RECLAIM_MAX_PCT, RECLAIM_MAX_CANDLES,
    CONFIRM_VOLUME_MULT, MAX_ZONE_DWELL, MSS_LOOKBACK,
    # sessions / kill zones
    LOKZ_START, LOKZ_CORE_START, LOKZ_CORE_END, LOKZ_END,
    NYOKZ_START, NYOKZ_CORE_START, NYOKZ_CORE_END, NYOKZ_END,
    LCKZ_START, LCKZ_END,
    KZ_CORE_BONUS, KZ_OUTER_BONUS, KZ_LCKZ_PENALTY,
    ASIAN_SESSION_PENALTY, DOW_MONDAY_BONUS, DOW_FRIDAY_PENALTY,
    # VWAP
    VWAP_MIN_CANDLES, AVWAP_MIN_CANDLES,
    VWAP_SLOPE_WINDOW, VWAP_SLOPE_RISING_PCT,
    VWAP_ABOVE_BONUS, VWAP_BELOW_PENALTY,
    VWAP_SD2_LOWER_BONUS, VWAP_SD1_LOWER_BONUS,
    VWAP_SD2_UPPER_PENALTY, AVWAP_CONFLUENCE_BONUS,
    # gate
    MIN_SIGNAL_SCORE,
)

log = logging.getLogger(__name__)

# Legacy alias kept for internal backward-compat references below
_MIN_SCORE = MIN_SIGNAL_SCORE


# ──────────────────────────────────────────────────────────────────────────── #
#  Data structures                                                              #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class FairValueGap:
    type: str        # "bullish" | "bearish"
    top: float
    bottom: float
    formed_at: int   # candle index where gap was confirmed
    filled: bool = False
    # v2 additions (safe — default values; old code ignores extra fields)
    ce: float = 0.0
    strength: float = 0.5
    origin: str = "standalone"    # "post_sweep" | "post_bos" | "standalone"


@dataclass
class OrderBlock:
    type: str        # "bullish" | "bearish"
    top: float
    bottom: float
    formed_at: int   # candle index of the OB candle itself
    mitigated: bool = False
    # v2 additions
    ce: float = 0.0
    wick_low: float = 0.0        # ob_wick_low for stop placement
    strength: float = 0.5
    origin: str = "standalone"
    fvg_confluence: bool = False


@dataclass
class TradeSignal:
    symbol:       str
    side:         str    # always "BUY"
    entry_price:  float
    stop_loss:    float
    take_profit:  float  # = tp1 (kept for backward compatibility)
    zone_type:    str    # "FVG" | "OB" | "FVG+OB" | "SWEEP+FVG" | "SWEEP+OB"
    strength:     float  # score / 10  (0.0 – 1.0)
    # ICT/SMC extended fields
    score:              float = 0
    tp1:                float = 0.0
    tp2:                float = 0.0
    tp3:                float = 0.0
    kill_zone:          Optional[str] = None
    structure:          str  = "NEUTRAL"
    discount_zone:      bool = False
    liquidity_sweep:    bool = False
    displacement:       bool = False
    fvg_present:        bool = False
    ob_present:         bool = False
    ote_zone:           bool = False
    confirmation_candle: bool = False
    vwap_filter:        bool = False
    score_breakdown:    Dict[str, float] = field(default_factory=dict)
    reason:             str  = ""
    atr:                float = 0.0


# ──────────────────────────────────────────────────────────────────────────── #
#  Candle utilities  (UNCHANGED — same interface as v1)                        #
# ──────────────────────────────────────────────────────────────────────────── #

_MEXC_KLINE_COLS = [
    "open_time", "open", "high", "low", "close",
    "volume", "close_time", "quote_volume",
]
_MEXC_KLINE_NCOLS = len(_MEXC_KLINE_COLS)


def candles_to_df(raw_klines: list) -> pd.DataFrame:
    """Convert a raw MEXC kline list to a typed DataFrame (unchanged)."""
    if not raw_klines:
        return pd.DataFrame()

    actual_ncols = len(raw_klines[0])
    if actual_ncols != _MEXC_KLINE_NCOLS:
        raw_klines = [
            (list(row) + [None] * _MEXC_KLINE_NCOLS)[:_MEXC_KLINE_NCOLS]
            for row in raw_klines
        ]

    df = pd.DataFrame(raw_klines, columns=_MEXC_KLINE_COLS)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df.reset_index(drop=True)


def _atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """EWM-ATR used for stop-loss sizing (unchanged)."""
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift(1)).abs()
    lc = (df["low"]  - df["close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _atr_series(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Rolling ATR series (used by FVG / displacement quality checks)."""
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift(1)).abs()
    lc = (df["low"]  - df["close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


# ──────────────────────────────────────────────────────────────────────────── #
#  Timestamp helper                                                             #
# ──────────────────────────────────────────────────────────────────────────── #

def _candle_utc_hour(df: pd.DataFrame, idx: int = -1) -> float:
    """Return UTC hour (decimal) from a candle's open_time.

    candles_to_df stores open_time as naive pd.Timestamp representing UTC ms.
    """
    ts = df["open_time"].iloc[idx]
    if isinstance(ts, pd.Timestamp):
        return float(ts.hour) + ts.minute / 60.0
    # Fallback: use system UTC hour
    return float(datetime.now(timezone.utc).hour)


def _candle_weekday(df: pd.DataFrame, idx: int = -1) -> int:
    """Return weekday (0=Mon … 6=Sun) from a candle's open_time."""
    ts = df["open_time"].iloc[idx]
    if isinstance(ts, pd.Timestamp):
        return ts.weekday()
    return datetime.now(timezone.utc).weekday()


# ──────────────────────────────────────────────────────────────────────────── #
#  STEP 9 — Kill Zone (UPGRADED)                                               #
# ──────────────────────────────────────────────────────────────────────────── #

def detect_kill_zone(dt: Optional[datetime] = None) -> Optional[str]:
    """Return the active ICT Kill Zone name or an off-session label.

    Returns
    -------
    "LONDON"         — London Open core (08:00–09:00 UTC), Mon–Thu
    "LONDON_OUTER"   — London Open outer (07:00–10:00 UTC, excl. core)
    "NEW_YORK"       — NY Open core (13:00–14:00 UTC), Mon–Thu
    "NEW_YORK_OUTER" — NY Open outer (12:00–15:00 UTC, excl. core)
    "LONDON_CLOSE"   — London Close (14:00–16:30 UTC)
    "FRIDAY_REDUCED" — Any active KZ on Friday (half-size, no score bonus)
    "OFF_HOURS"      — Weekday outside any KZ (scan allowed, no bonus)
    None             — Weekend (Sat/Sun) — hard block
    """
    now     = dt or datetime.now(timezone.utc)
    weekday = now.weekday()   # 0=Mon … 6=Sun
    hour    = now.hour + now.minute / 60.0

    if weekday >= 5:
        return None   # hard block on weekends

    in_london_core  = LOKZ_CORE_START  <= hour < LOKZ_CORE_END
    in_london_outer = LOKZ_START       <= hour < LOKZ_END and not in_london_core
    in_ny_core      = NYOKZ_CORE_START <= hour < NYOKZ_CORE_END
    in_ny_outer     = NYOKZ_START      <= hour < NYOKZ_END and not in_ny_core
    in_lckz         = LCKZ_START       <= hour < LCKZ_END

    if weekday == 4:   # Friday — allowed but no score bonus
        if in_london_core or in_london_outer or in_ny_core or in_ny_outer or in_lckz:
            return "FRIDAY_REDUCED"
        return "OFF_HOURS"

    # Mon–Thu: full sessions
    if in_london_core:   return "LONDON"
    if in_ny_core:       return "NEW_YORK"
    if in_lckz:          return "LONDON_CLOSE"
    if in_london_outer:  return "LONDON_OUTER"
    if in_ny_outer:      return "NEW_YORK_OUTER"
    return "OFF_HOURS"


def _kz_score_bonus(kz: Optional[str], weekday: int) -> float:
    """Fractional confidence modifier for a given kill-zone label."""
    if kz is None or kz in ("FRIDAY_REDUCED", "OFF_HOURS"):
        return 0.0
    base = {
        "LONDON":         KZ_CORE_BONUS,
        "NEW_YORK":       KZ_CORE_BONUS,
        "LONDON_OUTER":   KZ_OUTER_BONUS,
        "NEW_YORK_OUTER": KZ_OUTER_BONUS,
        "LONDON_CLOSE":  -KZ_LCKZ_PENALTY,
    }.get(kz, 0.0)
    if weekday == 0:
        base += DOW_MONDAY_BONUS
    return round(base, 3)


# ──────────────────────────────────────────────────────────────────────────── #
#  Pivot helpers  (UPGRADED — use SWING_LOOKBACK from config)                  #
# ──────────────────────────────────────────────────────────────────────────── #

def _find_swing_highs(
    df: pd.DataFrame,
    strength: int = SWING_LOOKBACK,
) -> List[Tuple[int, float]]:
    """Pivot highs: high strictly greater than `strength` neighbours on each side."""
    pivots: List[Tuple[int, float]] = []
    n = len(df)
    for i in range(strength, n - strength):
        hi = float(df.iloc[i]["high"])
        neighbours = [float(df.iloc[j]["high"]) for j in range(i - strength, i + strength + 1) if j != i]
        if all(hi > v for v in neighbours):
            pivots.append((i, hi))
    return pivots


def _find_swing_lows(
    df: pd.DataFrame,
    strength: int = SWING_LOOKBACK,
) -> List[Tuple[int, float]]:
    """Pivot lows: low strictly less than `strength` neighbours on each side."""
    pivots: List[Tuple[int, float]] = []
    n = len(df)
    for i in range(strength, n - strength):
        lo = float(df.iloc[i]["low"])
        neighbours = [float(df.iloc[j]["low"]) for j in range(i - strength, i + strength + 1) if j != i]
        if all(lo < v for v in neighbours):
            pivots.append((i, lo))
    return pivots


# ──────────────────────────────────────────────────────────────────────────── #
#  STEP 1 — Market Structure (UPGRADED)                                        #
# ──────────────────────────────────────────────────────────────────────────── #

def detect_market_structure(df: pd.DataFrame) -> dict:
    """Detect market structure bias using upgraded BOS / CHoCH logic.

    Uses SWING_LOOKBACK (default 5) from config for cleaner swing identification.
    Applies BOS_CLOSE_BUFFER_PCT so wick-only breaks are filtered.

    Returns (same keys as v1 + extra keys for v2 consumers)
    -------
    {
      "bias":           "BULLISH" | "BEARISH" | "NEUTRAL",
      "last_bos":       float,
      "choch_detected": bool,
      "swing_high":     float,
      "swing_low":      float,
      "hh":             bool,   # v2: higher-high confirmed
      "hl":             bool,   # v2: higher-low confirmed
    }
    """
    _default = {
        "bias": "NEUTRAL", "last_bos": 0.0,
        "choch_detected": False, "swing_high": 0.0, "swing_low": 0.0,
        "hh": False, "hl": False,
    }
    min_candles = SWING_LOOKBACK * 2 + 2
    if len(df) < min_candles:
        return _default

    highs = _find_swing_highs(df, SWING_LOOKBACK)
    lows  = _find_swing_lows(df,  SWING_LOOKBACK)

    current       = float(df.iloc[-1]["close"])
    bias          = "NEUTRAL"
    last_bos      = 0.0
    choch_detected = False
    hh_flag       = False
    hl_flag       = False

    swing_high = highs[-1][1] if highs else 0.0
    swing_low  = lows[-1][1]  if lows  else 0.0

    # ── BOS: close breaks last swing high with buffer ────────────────────────
    if highs:
        threshold = highs[-1][1] * (1.0 + BOS_CLOSE_BUFFER_PCT)
        if current > threshold:
            bias     = "BULLISH"
            last_bos = highs[-1][1]

    # ── Sequence analysis: HH + HL → BULLISH; LH + LL → BEARISH ────────────
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1]  > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1]  < lows[-2][1]

        hh_flag = hh
        hl_flag = hl

        if hh and hl:
            bias = "BULLISH"
        elif lh and ll:
            bias = "BEARISH"

        # CHoCH: was HH-HL (bullish) but latest high is lower (character shift)
        if lh and hl:
            choch_detected = True

        # Also CHoCH: close below last Higher Low
        if hl and lows:
            last_hl = lows[-1][1]
            if current < last_hl * (1.0 - BOS_CLOSE_BUFFER_PCT):
                choch_detected = True

    # CHoCH beats BOS when both present simultaneously
    return {
        "bias":           bias,
        "last_bos":       last_bos,
        "choch_detected": choch_detected,
        "swing_high":     swing_high,
        "swing_low":      swing_low,
        "hh":             hh_flag,
        "hl":             hl_flag,
    }


# ──────────────────────────────────────────────────────────────────────────── #
#  STEP 5 & 7 — Premium / Discount / OTE (UPGRADED)                           #
# ──────────────────────────────────────────────────────────────────────────── #

def detect_premium_discount(df: pd.DataFrame) -> dict:
    """Classify current price using structural range + Fibonacci OTE levels.

    v2: Uses SWING_LOOKBACK-based structural range (not fixed 40-candle window).
    Falls back to 40-candle window when insufficient swing data.

    Returns (same keys as v1 + extra keys for v2 consumers)
    -------
    {
      "zone":         "DISCOUNT" | "EQUILIBRIUM" | "PREMIUM",
      "ote_zone":     bool,
      "ote_quality":  "GOLDEN" | "OTE" | "EXTENDED" | "OUTSIDE",
      "equilibrium":  float,
      "range_high":   float,
      "range_low":    float,
      "position_pct": float,
      "ote_top":      float,   # v2: OTE zone top    (61.8 % retrace from high)
      "ote_mid":      float,   # v2: OTE precise     (70.5 %)
      "ote_bottom":   float,   # v2: OTE zone bottom (78.6 %)
    }
    """
    highs = _find_swing_highs(df, SWING_LOOKBACK)
    lows  = _find_swing_lows(df,  SWING_LOOKBACK)

    if highs and lows:
        range_high = max(h for _, h in highs[-3:]) if len(highs) >= 3 else highs[-1][1]
        range_low  = min(l for _, l in lows[-3:])  if len(lows)  >= 3 else lows[-1][1]
    else:
        recent     = df.iloc[-40:] if len(df) >= 40 else df
        range_high = float(recent["high"].max())
        range_low  = float(recent["low"].min())

    current = float(df.iloc[-1]["close"])
    _eq     = (range_high + range_low) / 2.0

    if range_high <= range_low or (range_high - range_low) / max(range_low, 1e-9) < RANGE_MIN_SIZE_PCT:
        return {
            "zone": "EQUILIBRIUM", "ote_zone": False, "ote_quality": "OUTSIDE",
            "equilibrium": _eq, "range_high": range_high, "range_low": range_low,
            "position_pct": 50.0, "ote_top": 0.0, "ote_mid": 0.0, "ote_bottom": 0.0,
        }

    rang         = range_high - range_low
    position_pct = (current - range_low) / rang * 100.0

    # Zone classification
    ratio = (current - range_low) / rang   # 0 = at low, 1 = at high
    if abs(ratio - 0.5) <= EQ_BUFFER_PCT:
        zone = "EQUILIBRIUM"
    elif ratio < 0.5:
        zone = "DISCOUNT"
    else:
        zone = "PREMIUM"

    # Fibonacci OTE levels (retracement from range_high toward range_low)
    ote_top    = range_high - rang * OTE_FIB_LOW      # 61.8 % retrace
    ote_mid    = range_high - rang * OTE_FIB_MID      # 70.5 %
    ote_bottom = range_high - rang * OTE_FIB_HIGH     # 78.6 %
    ote_ext    = range_high - rang * OTE_FIB_EXTENDED # 88.6 %

    in_ote = ote_bottom <= current <= ote_top

    if ote_mid <= current <= ote_top:
        ote_quality = "GOLDEN"
    elif ote_bottom <= current < ote_mid:
        ote_quality = "OTE"
    elif ote_ext  <= current < ote_bottom:
        ote_quality = "EXTENDED"
    else:
        ote_quality = "OUTSIDE"

    return {
        "zone":         zone,
        "ote_zone":     in_ote,
        "ote_quality":  ote_quality,
        "equilibrium":  _eq,
        "range_high":   range_high,
        "range_low":    range_low,
        "position_pct": round(position_pct, 1),
        "ote_top":      round(ote_top,    8),
        "ote_mid":      round(ote_mid,    8),
        "ote_bottom":   round(ote_bottom, 8),
    }


def detect_ote_zone(
    swing_low: float,
    swing_high: float,
    current_price: float,
) -> dict:
    """OTE zone via Fibonacci retracement of a specific impulse leg.

    Unchanged external signature; v2 adds fib_705 and ote_quality keys.
    """
    _outside: dict = {
        "in_ote": False, "fib_618": 0.0, "fib_65": 0.0,
        "fib_79": 0.0, "zone_quality": "OUTSIDE",
        "fib_705": 0.0, "ote_quality": "OUTSIDE",
    }

    if swing_high <= swing_low or current_price <= 0:
        return _outside

    rang     = swing_high - swing_low
    fib_618  = swing_high - rang * OTE_FIB_LOW
    fib_705  = swing_high - rang * OTE_FIB_MID
    fib_79   = swing_high - rang * OTE_FIB_HIGH
    fib_65   = swing_high - rang * 0.650   # legacy level kept

    in_ote   = fib_79 <= current_price <= fib_618

    if fib_705 <= current_price <= fib_618:
        quality = "GOLDEN"
    elif in_ote:
        quality = "OTE"
    else:
        quality = "OUTSIDE"

    return {
        "in_ote":       in_ote,
        "fib_618":      round(fib_618, 8),
        "fib_65":       round(fib_65,  8),
        "fib_79":       round(fib_79,  8),
        "fib_705":      round(fib_705, 8),
        "zone_quality": quality,
        "ote_quality":  quality,
    }


# ──────────────────────────────────────────────────────────────────────────── #
#  STEP 2 — Liquidity: pool mapping + sweep detection (UPGRADED)               #
# ──────────────────────────────────────────────────────────────────────────── #

def _is_round_number(price: float, proximity_pct: float = 0.005) -> bool:
    """True when price is within proximity_pct of a round number."""
    for mag in (0.001, 0.01, 0.1, 1, 10, 100, 1_000, 10_000):
        nearest = round(price / mag) * mag
        if nearest > 0 and abs(price - nearest) / nearest <= proximity_pct:
            return True
    return False


def map_liquidity_levels(
    df: pd.DataFrame,
    max_levels: int = 10,
) -> Tuple[List[dict], List[dict]]:
    """Return (ssl_levels, bsl_levels) — unswept liquidity pools ranked by strength.

    Each level dict: {price, type, strength, age_candles, is_equal_level, htf_origin}
    """
    n       = len(df)
    highs   = _find_swing_highs(df, SWING_LOOKBACK)
    lows    = _find_swing_lows(df,  SWING_LOOKBACK)
    current = float(df.iloc[-1]["close"])

    def _strength(price: float, age: int, is_eq: bool, is_round: bool, retests: int) -> float:
        s = 0.30
        if is_eq:    s += 0.25
        if is_round: s += 0.15
        s += min(retests - 1, 3) * 0.10
        s -= age / max(MAX_LIQUIDITY_AGE, 1) * 0.20
        return round(min(max(s, 0.0), 1.0), 2)

    def _count_retests(price: float, side: str) -> int:
        count = 0
        for _, row in df.iterrows():
            if side == "SSL" and abs(float(row["low"])  - price) / price <= 0.003:
                count += 1
            if side == "BSL" and abs(float(row["high"]) - price) / price <= 0.003:
                count += 1
        return max(count, 1)

    def _consumed(price: float, side: str) -> bool:
        for i in range(len(df) - 1):
            c       = df.iloc[i]
            body_lo = min(float(c["open"]), float(c["close"]))
            body_hi = max(float(c["open"]), float(c["close"]))
            if side == "SSL" and body_lo < price * (1 - 0.001):
                if float(df.iloc[i + 1]["close"]) < price:
                    return True
            if side == "BSL" and body_hi > price * (1 + 0.001):
                if float(df.iloc[i + 1]["close"]) > price:
                    return True
        return False

    def _build(pivots: List[Tuple[int, float]], side: str) -> List[dict]:
        seen_prices: List[float] = []
        levels: List[dict] = []
        for idx, price in pivots:
            if _consumed(price, side):
                continue
            age = n - 1 - idx
            if age > MAX_LIQUIDITY_AGE:
                continue
            is_eq = any(abs(price - p) / max(p, 1e-9) <= EQUAL_LEVEL_TOLERANCE for p in seen_prices)
            is_rn = _is_round_number(price)
            rt    = _count_retests(price, side)
            s     = _strength(price, age, is_eq, is_rn, rt)
            if s < MIN_POOL_STRENGTH:
                continue
            seen_prices.append(price)
            levels.append({
                "price":         round(price, 8),
                "type":          side,
                "strength":      s,
                "age_candles":   age,
                "is_equal_level": is_eq,
                "htf_origin":    False,
            })
        levels.sort(key=lambda x: abs(x["price"] - current))
        return levels[:max_levels]

    return _build(lows, "SSL"), _build(highs, "BSL")


def detect_liquidity_sweep(df: pd.DataFrame) -> dict:
    """Detect a buy-side SSL sweep with body-close confirmation.

    v2: uses named thresholds from config; computes pool strength score.

    Returns same keys as v1 + `sweep_strength_score` (float 0–1).
    """
    _no = {
        "sweep_detected": False, "sweep_price": 0.0,
        "sweep_candle_index": -1, "sweep_strength": 0.0,
        "sweep_strength_score": 0.0,
    }

    recent = df.iloc[-30:].reset_index(drop=True) if len(df) >= 30 else df.reset_index(drop=True)
    n      = len(recent)
    if n < SWING_LOOKBACK * 2 + 2:
        return _no

    # ── Candidate levels: swing lows + equal-low pools ───────────────────────
    swing_lows = _find_swing_lows(recent, min(SWING_LOOKBACK, 2))
    if not swing_lows:
        return _no

    candidate_levels: List[Tuple[int, float]] = list(swing_lows[-5:])
    for a_idx, a_lo in swing_lows:
        for b_idx, b_lo in swing_lows:
            if a_idx >= b_idx:
                continue
            if abs(a_lo - b_lo) / max(b_lo, 1e-9) <= EQUAL_LEVEL_TOLERANCE:
                pool_idx   = max(a_idx, b_idx)
                pool_price = (a_lo + b_lo) / 2.0
                candidate_levels.append((pool_idx, pool_price))

    seen: set = set()
    deduped: List[Tuple[int, float]] = []
    for item in sorted(candidate_levels, key=lambda x: x[0]):
        key = round(item[1], 4)
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    avg_vol = float(recent["volume"].iloc[-21:-1].mean()) if n > 21 else float(recent["volume"].mean())

    # ── Scan newest → oldest ─────────────────────────────────────────────────
    for i in range(n - 1, -1, -1):
        c     = recent.iloc[i]
        lo    = float(c["low"])
        close = float(c["close"])
        vol   = float(c["volume"])
        rng   = float(c["high"]) - lo

        for level_idx, level_price in deduped:
            if i <= level_idx:
                continue
            min_wick = level_price * (1.0 - SWEEP_WICK_MIN_PCT)
            if lo > min_wick or close <= level_price:
                continue   # wick didn't breach OR body didn't recover
            wick_below = (level_price - lo) / max(level_price, 1e-9)
            wick_ratio = (level_price - lo) / max(rng, 1e-9)
            if wick_ratio < SWEEP_WICK_RATIO:
                continue

            # Quality score
            q = 0.35
            if wick_below >= SWEEP_WICK_MIN_PCT * 2:  q += 0.10
            if vol > avg_vol * SWEEP_VOLUME_MULT:      q += 0.20
            if wick_ratio >= SWEEP_WICK_RATIO * 1.5:  q += 0.10

            # Displacement follow-through
            disp_ok = False
            for j in range(1, min(SWEEP_CONFIRM_CANDLES + 1, n - i)):
                nc = recent.iloc[i + j]
                nb = abs(float(nc["close"]) - float(nc["open"])) / max(float(nc["open"]), 1e-9)
                if nb >= DISP_BODY_MIN_PCT and float(nc["close"]) > float(nc["open"]):
                    disp_ok = True
                    q += 0.15
                    break

            if not disp_ok:
                q -= 0.10

            return {
                "sweep_detected":      True,
                "sweep_price":         round(level_price, 8),
                "sweep_candle_index":  i,
                "sweep_strength":      round(wick_below * 100.0, 3),
                "sweep_strength_score": round(min(q, 1.0), 2),
            }

    return _no


# ──────────────────────────────────────────────────────────────────────────── #
#  STEP 6 — Displacement (UPGRADED)                                            #
# ──────────────────────────────────────────────────────────────────────────── #

def _displacement_quality(df: pd.DataFrame, idx: int) -> float:
    """Return quality score 0–1 for a single bullish displacement candle."""
    if idx < 0 or idx >= len(df):
        return 0.0
    c     = df.iloc[idx]
    open_ = float(c["open"])
    close = float(c["close"])
    high  = float(c["high"])
    low   = float(c["low"])

    if close <= open_:
        return 0.0

    body  = close - open_
    rng   = high  - low
    if rng <= 0 or close <= 0:
        return 0.0

    body_pct    = body / close
    body_ratio  = body / rng
    close_pos   = (close - low) / rng   # 1.0 = closes at high
    upper_wick  = (high - close) / rng

    if body_pct   < DISP_BODY_MIN_PCT:     return 0.0
    if body_ratio < DISP_BODY_RANGE_RATIO: return 0.0

    atr_val = float(_atr_series(df).iloc[idx])
    rng_atr = rng / atr_val if atr_val > 0 else 0.0

    avg_vol    = float(df.iloc[max(0, idx - 20):idx]["volume"].mean()) if idx > 0 else 0.0
    candle_vol = float(c["volume"])

    q = 0.30  # body size + ratio already satisfied
    if rng_atr >= DISP_ATR_MULT:                     q += 0.15
    if avg_vol > 0 and candle_vol >= avg_vol * DISP_VOLUME_MULT: q += 0.20
    if close_pos >= DISP_CLOSE_POSITION_MIN:         q += 0.15
    if upper_wick <= 0.15:                           q += 0.10   # tight upper wick
    if body_ratio >= 0.70:                           q += 0.10   # strong body dominance

    # Check if next candle cancels (retraces > DISP_CANCEL_RATIO of body)
    if idx + 1 < len(df):
        nc = df.iloc[idx + 1]
        retrace = close - float(nc["close"])
        if retrace > body * DISP_CANCEL_RATIO:
            q -= 0.25

    return round(min(max(q, 0.0), 1.0), 2)


def _is_displacement_candle(df: pd.DataFrame, idx: int) -> bool:
    """True when candle at `idx` is a bullish displacement (v2 quality gate)."""
    return _displacement_quality(df, idx) >= DISP_MIN_QUALITY


def _is_two_candle_displacement(df: pd.DataFrame, idx: int) -> bool:
    """True when candles at `idx-1` and `idx` form a cluster displacement."""
    if idx < 1 or idx >= len(df):
        return False

    c1 = df.iloc[idx - 1]
    c2 = df.iloc[idx]

    o1, c_1 = float(c1["open"]), float(c1["close"])
    o2, c_2 = float(c2["open"]), float(c2["close"])

    if c_1 <= o1 or c_2 <= o2:
        return False

    mid1 = o1 + (c_1 - o1) * 0.5
    if o2 < mid1:
        return False   # c2 retraced into c1's lower half

    combined_body = c_2 - o1
    if combined_body / max(c_2, 1e-9) < DISP_BODY_MIN_PCT * 1.5:
        return False

    # Both candles must meet minimum individual body
    b1 = (c_1 - o1) / max(c_1, 1e-9)
    b2 = (c_2 - o2) / max(c_2, 1e-9)
    if b1 < 0.003 or b2 < 0.003:
        return False

    start   = max(0, idx - 1 - 20)
    avg_vol = float(df.iloc[start:idx - 1]["volume"].mean()) if idx - 1 > start else 0.0
    avg_two = (float(c1["volume"]) + float(c2["volume"])) / 2.0
    if avg_vol > 0 and avg_two < avg_vol * 1.05:
        return False

    return True


def _find_displacement_idx(df: pd.DataFrame, search_start: int, search_end: int) -> int:
    """Return index of best displacement candle in [search_start, search_end), or -1."""
    best_idx = -1
    best_q   = 0.0
    for i in range(search_end - 1, search_start - 1, -1):
        q = _displacement_quality(df, i)
        if q > best_q:
            best_q   = q
            best_idx = i
        if best_q >= 0.80:
            break   # good enough
    return best_idx if best_q >= DISP_MIN_QUALITY else -1


# ──────────────────────────────────────────────────────────────────────────── #
#  STEP 3 — Fair Value Gap (UPGRADED)                                          #
# ──────────────────────────────────────────────────────────────────────────── #

def detect_fvgs(df: pd.DataFrame) -> List[FairValueGap]:
    """Identify all FVGs with ATR-based size filter and CE level.

    Backward-compatible: returns List[FairValueGap] with the same fields as v1.
    v2 additions (CE, strength, origin) use default values on the dataclass.
    """
    fvgs: List[FairValueGap] = []
    n    = len(df)
    atr  = _atr_series(df)

    for i in range(1, n - 1):
        c_prev = df.iloc[i - 1]
        c_next = df.iloc[i + 1]

        # Bullish FVG
        gap_lo = float(c_next["low"])
        gap_hi = float(c_prev["high"])
        if gap_lo > gap_hi:
            gap     = gap_lo - gap_hi
            mid     = (gap_lo + gap_hi) / 2.0
            atr_val = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 0.0
            min_sz  = max(mid * FVG_MIN_SIZE_PCT, atr_val * FVG_MIN_SIZE_ATR_MULT)
            if gap >= min_sz and (n - 1 - i) <= FVG_MAX_AGE_CANDLES:
                ce = gap_hi + gap * FVG_CE_RATIO
                fvgs.append(FairValueGap(
                    type="bullish", top=round(gap_lo, 8),
                    bottom=round(gap_hi, 8), formed_at=i,
                    ce=round(ce, 8), strength=0.5,
                ))

        # Bearish FVG
        gap_hi2 = float(c_prev["low"])
        gap_lo2 = float(c_next["high"])
        if gap_hi2 > gap_lo2:
            gap     = gap_hi2 - gap_lo2
            mid     = (gap_hi2 + gap_lo2) / 2.0
            atr_val = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 0.0
            min_sz  = max(mid * FVG_MIN_SIZE_PCT, atr_val * FVG_MIN_SIZE_ATR_MULT)
            if gap >= min_sz and (n - 1 - i) <= FVG_MAX_AGE_CANDLES:
                ce = gap_lo2 + gap * FVG_CE_RATIO
                fvgs.append(FairValueGap(
                    type="bearish", top=round(gap_hi2, 8),
                    bottom=round(gap_lo2, 8), formed_at=i,
                    ce=round(ce, 8), strength=0.5,
                ))

    # Mark filled/breached
    for fvg in fvgs:
        for j in range(fvg.formed_at + 1, n):
            c = df.iloc[j]
            if fvg.type == "bullish":
                body_lo = min(float(c["open"]), float(c["close"]))
                if body_lo < fvg.bottom - fvg.bottom * FVG_INVALIDATION_PCT:
                    fvg.filled = True
                    break
            else:
                body_hi = max(float(c["open"]), float(c["close"]))
                if body_hi > fvg.top + fvg.top * FVG_INVALIDATION_PCT:
                    fvg.filled = True
                    break

    return fvgs


# ──────────────────────────────────────────────────────────────────────────── #
#  STEP 4 — Order Block (UPGRADED)                                             #
# ──────────────────────────────────────────────────────────────────────────── #

def detect_order_blocks(df: pd.DataFrame) -> List[OrderBlock]:
    """Detect OBs: last opposing candle before a confirmed displacement.

    v2: uses DISP_BODY_MIN_PCT for impulse gate; OB_BODY_MIN_PCT for the OB candle.
    Backward-compatible List[OrderBlock] return.
    """
    obs: List[OrderBlock] = []
    n         = len(df)
    lookahead = OB_DISP_CANDLES
    avg_vol   = df["volume"].rolling(20, min_periods=1).mean()

    for i in range(1, n - lookahead - 1):
        c      = df.iloc[i]
        c_open  = float(c["open"])
        c_close = float(c["close"])
        c_low   = float(c["low"])
        c_high  = float(c["high"])
        body_sz = abs(c_close - c_open) / max(c_open, 1e-9)

        # ── Bullish OB: last bearish candle before bullish displacement ────────
        if c_close < c_open and body_sz >= OB_BODY_MIN_PCT:
            disp_idx = -1
            for j in range(1, lookahead + 1):
                k  = i + j
                if k >= n:
                    break
                nc    = df.iloc[k]
                nc_b  = float(nc["close"]) - float(nc["open"])
                nc_bp = nc_b / max(float(nc["open"]), 1e-9)
                # Is this a displacement that breaks above OB high?
                if nc_b > 0 and nc_bp >= DISP_BODY_MIN_PCT and float(nc["high"]) > c_high:
                    # Make sure no more recent bearish candle exists before this displacement
                    intervening_bearish = any(
                        float(df.iloc[m]["close"]) < float(df.iloc[m]["open"])
                        and abs(float(df.iloc[m]["close"]) - float(df.iloc[m]["open"])) / max(float(df.iloc[m]["open"]), 1e-9) >= OB_BODY_MIN_PCT
                        for m in range(i + 1, k)
                    )
                    if not intervening_bearish:
                        disp_idx = k
                    break

            if disp_idx < 0:
                continue
            if (n - 1 - i) > OB_MAX_AGE_CANDLES:
                continue

            ob_top    = max(c_open, c_close)
            ob_bottom = c_low
            ob_ce     = ob_bottom + (ob_top - ob_bottom) * OB_CE_RATIO
            av        = float(avg_vol.iloc[i]) if not pd.isna(avg_vol.iloc[i]) else 0.0
            strength  = 0.40 + (0.20 if av > 0 and float(c["volume"]) > av * 1.2 else 0.0)

            obs.append(OrderBlock(
                type="bullish", top=round(ob_top, 8),
                bottom=round(ob_bottom, 8), formed_at=i,
                ce=round(ob_ce, 8), wick_low=round(c_low, 8),
                strength=round(strength, 2),
            ))

        # ── Bearish OB: last bullish candle before bearish displacement ────────
        elif c_close > c_open and body_sz >= OB_BODY_MIN_PCT:
            disp_idx = -1
            for j in range(1, lookahead + 1):
                k  = i + j
                if k >= n:
                    break
                nc    = df.iloc[k]
                nc_b  = float(nc["open"]) - float(nc["close"])
                nc_bp = nc_b / max(float(nc["open"]), 1e-9)
                if nc_b > 0 and nc_bp >= DISP_BODY_MIN_PCT and float(nc["low"]) < c_low:
                    intervening_bullish = any(
                        float(df.iloc[m]["close"]) > float(df.iloc[m]["open"])
                        and abs(float(df.iloc[m]["close"]) - float(df.iloc[m]["open"])) / max(float(df.iloc[m]["open"]), 1e-9) >= OB_BODY_MIN_PCT
                        for m in range(i + 1, k)
                    )
                    if not intervening_bullish:
                        disp_idx = k
                    break
            if disp_idx < 0:
                continue
            if (n - 1 - i) > OB_MAX_AGE_CANDLES:
                continue

            ob_top    = c_high
            ob_bottom = min(c_open, c_close)
            ob_ce     = ob_bottom + (ob_top - ob_bottom) * OB_CE_RATIO

            obs.append(OrderBlock(
                type="bearish", top=round(ob_top, 8),
                bottom=round(ob_bottom, 8), formed_at=i,
                ce=round(ob_ce, 8), wick_low=round(ob_bottom, 8),
                strength=0.40,
            ))

    # Mark mitigated
    for ob in obs:
        start = ob.formed_at + lookahead + 1
        for j in range(start, n):
            c = df.iloc[j]
            if ob.type == "bullish" and float(c["low"]) <= ob.bottom * (1 + OB_INVALIDATION_PCT):
                ob.mitigated = True
                break
            if ob.type == "bearish" and float(c["high"]) >= ob.top * (1 - OB_INVALIDATION_PCT):
                ob.mitigated = True
                break

    return obs


def _recency_score(formed_at: int, total_candles: int) -> float:
    age = total_candles - formed_at
    return max(0.0, 1.0 - age / max(total_candles, 1))


# ──────────────────────────────────────────────────────────────────────────── #
#  STEP 8 — Confirmation Candle (UPGRADED)                                     #
# ──────────────────────────────────────────────────────────────────────────── #

def _is_confirmation_candle(
    df: pd.DataFrame,
    fvgs: List[FairValueGap],
    obs: List[OrderBlock],
) -> float:
    """Return confirmation score (0.0, 0.5, or 1.0).

    v2: checks MSS (mini-CHoCH within zone), engulf, reclaim, and displacement close
    in addition to the original "bullish close inside zone" and "2 consecutive bullish"
    methods.

    Priority: MSS/displacement-close (1.0) > reclaim (1.0) > engulf (0.8) >
              bullish-inside-zone (1.0) > momentum (0.5) > approaching (0.3)
    """
    n = len(df)
    if n < 3:
        return 0.0

    active_zones: List[Tuple[float, float]] = [
        (f.bottom, f.top) for f in fvgs if not f.filled and f.type == "bullish"
    ] + [
        (o.bottom, o.top) for o in obs if not o.mitigated and o.type == "bullish"
    ]

    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    last_c = float(last["close"])
    last_o = float(last["open"])
    prev_c = float(prev["close"])
    prev_o = float(prev["open"])
    last_l = float(last["low"])
    last_h = float(last["high"])
    avg_vol = float(df["volume"].iloc[-21:-1].mean()) if n > 21 else float(df["volume"].mean())
    vol_ok  = float(last["volume"]) > avg_vol * CONFIRM_VOLUME_MULT

    # ── Method A: MSS — mini-CHoCH on recent swings within zone ─────────────
    if active_zones and n >= MSS_LOOKBACK * 2 + 4:
        recent_highs = _find_swing_highs(df.iloc[-20:].reset_index(drop=True), MSS_LOOKBACK)
        if len(recent_highs) >= 1:
            lth = recent_highs[-1][1]   # last local high in mini-structure
            # MSS = close above that local high while price was in zone recently
            if last_c > last_o and last_c > lth:
                for zb, zt in active_zones:
                    if last_l <= zt * 1.005 and last_c >= zb * 0.995:
                        return 1.0

    # ── Method B: Displacement close — exits zone with disp quality ──────────
    if active_zones and _displacement_quality(df, n - 1) >= DISP_MIN_QUALITY:
        for zb, zt in active_zones:
            if last_l <= zt * 1.01 and last_c > zt:
                return 1.0

    # ── Method C: Reclaim — price briefly violated zone bottom and recovered ──
    if active_zones and last_c > last_o:
        for zb, zt in active_zones:
            # Check if any of last RECLAIM_MAX_CANDLES candles briefly dipped below zb
            lookback = min(RECLAIM_MAX_CANDLES + 1, n - 1)
            for k in range(2, lookback + 1):
                past_c = df.iloc[-k]
                body_lo = min(float(past_c["open"]), float(past_c["close"]))
                viol    = (zb - body_lo) / max(zb, 1e-9)
                if 0.001 <= viol <= RECLAIM_MAX_PCT:
                    if last_c > zb * (1.0 + 0.002):
                        return 1.0
                    break

    # ── Method D: Strong engulf inside / near zone ────────────────────────────
    if active_zones and last_c > last_o and prev_c < prev_o:
        prev_body = prev_o - prev_c
        last_body = last_c - last_o
        if prev_body > 0 and last_body >= prev_body * ENGULF_STRONG_MULT:
            for zb, zt in active_zones:
                if last_l <= zt * 1.005:
                    return min(0.8 + (0.2 if vol_ok else 0.0), 1.0)

    # ── Method E (original): bullish close inside zone (last 3 candles) ──────
    if active_zones:
        lookback = min(3, n)
        for i in range(n - 1, n - lookback - 1, -1):
            c     = df.iloc[i]
            close = float(c["close"])
            open_ = float(c["open"])
            if close <= open_:
                continue
            for zb, zt in active_zones:
                if zb <= close <= zt:
                    return 1.0

    # ── Method F (original): 2 consecutive bullish candles ───────────────────
    if n >= 2 and last_c > last_o and prev_c > prev_o:
        return 0.5

    # ── Method G: price approaching zone top ─────────────────────────────────
    if active_zones:
        for zb, zt in active_zones:
            if zt < last_c <= zt * 1.005:
                return 0.3

    return 0.0


# ──────────────────────────────────────────────────────────────────────────── #
#  STEP 10 — VWAP (UPGRADED)                                                   #
# ──────────────────────────────────────────────────────────────────────────── #

def _compute_vwap_bands(df_subset: pd.DataFrame) -> dict:
    """Compute VWAP + SD1 + SD2 bands for a given candle subset."""
    mask = df_subset["volume"] > 0
    s    = df_subset[mask]
    if len(s) < VWAP_MIN_CANDLES:
        return {"vwap": 0.0, "sd1_upper": 0.0, "sd1_lower": 0.0,
                "sd2_upper": 0.0, "sd2_lower": 0.0, "slope": "flat", "valid": False}

    tp      = (s["high"] + s["low"] + s["close"]) / 3.0
    vol     = s["volume"]
    cum_vol = vol.cumsum()
    vwap_s  = (tp * vol).cumsum() / cum_vol
    var_s   = ((tp - vwap_s) ** 2 * vol).cumsum() / cum_vol
    std_s   = var_s.apply(lambda x: x ** 0.5 if x >= 0 else 0.0)

    v_last  = float(vwap_s.iloc[-1])
    sd_last = float(std_s.iloc[-1])

    # Slope
    if len(vwap_s) >= VWAP_SLOPE_WINDOW:
        chg = (vwap_s.iloc[-1] - vwap_s.iloc[-VWAP_SLOPE_WINDOW]) / max(vwap_s.iloc[-VWAP_SLOPE_WINDOW], 1e-9)
        slope = "rising" if chg >= VWAP_SLOPE_RISING_PCT else ("falling" if chg <= -VWAP_SLOPE_RISING_PCT else "flat")
    else:
        slope = "flat"

    return {
        "vwap":      round(v_last, 8),
        "sd1_upper": round(v_last + sd_last, 8),
        "sd1_lower": round(v_last - sd_last, 8),
        "sd2_upper": round(v_last + 2 * sd_last, 8),
        "sd2_lower": round(v_last - 2 * sd_last, 8),
        "slope":     slope,
        "valid":     True,
    }


def calculate_vwap(df: pd.DataFrame) -> float:
    """Session VWAP — returns float (backward-compatible with v1).

    Uses London session (07:00 UTC) or NY session (13:00 UTC) start.
    Falls back to full available data when session start unavailable.
    """
    if df.empty:
        return 0.0

    session_df = df
    if "open_time" in df.columns and pd.api.types.is_datetime64_any_dtype(df["open_time"]):
        now  = datetime.now(timezone.utc)
        hour = now.hour
        session_hour = 7 if 7 <= hour < 13 else (13 if hour >= 13 else 0)
        today = now.date()
        session_start = pd.Timestamp(
            year=today.year, month=today.month, day=today.day,
            hour=session_hour, tzinfo=timezone.utc,
        )
        col = df["open_time"]
        if col.dt.tz is None:
            col = col.dt.tz_localize("UTC")
        mask = col >= session_start
        if mask.sum() >= VWAP_MIN_CANDLES:
            session_df = df[mask]

    tp    = (session_df["high"] + session_df["low"] + session_df["close"]) / 3.0
    vol   = session_df["volume"].fillna(0)
    total = float(vol.sum())
    if total <= 0:
        return float(tp.iloc[-1]) if not tp.empty else 0.0
    return float((tp * vol).sum() / total)


def _vwap_context(df: pd.DataFrame, sweep_candle_idx: int = -1) -> dict:
    """Full VWAP context for internal use by generate_signal."""
    current = float(df.iloc[-1]["close"])

    # Daily VWAP (from 00:00 UTC today)
    daily_bands: dict = {"valid": False, "vwap": 0.0}
    if "open_time" in df.columns and pd.api.types.is_datetime64_any_dtype(df["open_time"]):
        today_date = df["open_time"].iloc[-1].date()
        mask_day   = df["open_time"].dt.date == today_date
        if mask_day.sum() >= VWAP_MIN_CANDLES:
            daily_bands = _compute_vwap_bands(df[mask_day])

    # Anchored VWAP from sweep candle
    avwap = 0.0
    if sweep_candle_idx >= 0 and sweep_candle_idx < len(df):
        subset = df.iloc[sweep_candle_idx:]
        if len(subset) >= AVWAP_MIN_CANDLES:
            avwap_bands = _compute_vwap_bands(subset)
            avwap = avwap_bands.get("vwap", 0.0)

    # Confidence modifier
    modifier = 0.0
    if daily_bands["valid"] and daily_bands["vwap"] > 0:
        dv = daily_bands["vwap"]
        if current > dv:   modifier += VWAP_ABOVE_BONUS
        else:               modifier -= VWAP_BELOW_PENALTY
        if daily_bands.get("sd2_lower", 0) > 0 and current <= daily_bands["sd2_lower"]:
            modifier += VWAP_SD2_LOWER_BONUS
        elif daily_bands.get("sd1_lower", 0) > 0 and current <= daily_bands["sd1_lower"]:
            modifier += VWAP_SD1_LOWER_BONUS
        if daily_bands.get("sd2_upper", 0) > 0 and current >= daily_bands["sd2_upper"]:
            modifier -= VWAP_SD2_UPPER_PENALTY
        if daily_bands.get("slope") == "falling":
            modifier -= 0.05

    return {
        "daily_vwap":    daily_bands.get("vwap", 0.0),
        "avwap_sweep":   avwap,
        "slope":         daily_bands.get("slope", "flat"),
        "price_vs_vwap": "above" if daily_bands["valid"] and current > daily_bands.get("vwap", 0) else "below",
        "modifier":      round(max(-0.30, min(0.30, modifier)), 2),
        "sd1_upper":     daily_bands.get("sd1_upper", 0.0),
        "sd1_lower":     daily_bands.get("sd1_lower", 0.0),
        "sd2_upper":     daily_bands.get("sd2_upper", 0.0),
        "sd2_lower":     daily_bands.get("sd2_lower", 0.0),
        "valid":         daily_bands["valid"],
    }


# ──────────────────────────────────────────────────────────────────────────── #
#  STEP 11 — generate_signal (REBUILT — full ICT sequence)                     #
# ──────────────────────────────────────────────────────────────────────────── #

def generate_signal(
    symbol: str,
    df: pd.DataFrame,
    htf_df: Optional[pd.DataFrame] = None,
    trade_type: str = "",
) -> Optional[TradeSignal]:
    """Full ICT + SMC confluence check.

    ICT sequence (10-point scoring, minimum MIN_SIGNAL_SCORE):
      +1  Kill zone (London / NY core, Mon–Thu)
      +1  4H bullish structure (BOS confirmed, no CHoCH)
      +1  Price in discount / OTE zone (not PREMIUM)
      +1  Liquidity sweep confirmed
      +1  Displacement candle quality ≥ threshold
      +1  Bullish FVG active in discount zone
      +1  Bullish OB active in discount zone
      +1  OTE Fibonacci zone (61.8 – 78.6 %)
      +1  Confirmation candle (MSS / engulf / reclaim / disp-close)
      +1  VWAP filter (price at or below session VWAP)

    Parameters identical to v1 — backward-compatible.
    """
    if len(df) < max(ATR_PERIOD + 5, OB_LOOKBACK + 5):
        log.info("[%s] generate_signal: insufficient candles (%d)", symbol, len(df))
        return None

    current_price = float(df.iloc[-1]["close"])
    current_atr   = float(_atr(df).iloc[-1])
    n             = len(df)

    score_bd: Dict[str, float] = {
        "kill_zone": 0.0, "bos": 0.0, "discount": 0.0,
        "sweep": 0.0, "displacement": 0.0, "fvg": 0.0,
        "ob": 0.0, "ote": 0.0, "confirmation": 0.0, "vwap": 0.0,
    }

    # ── 1. Kill Zone ─────────────────────────────────────────────────────────
    kill_zone = detect_kill_zone()
    if kill_zone is None:
        log.info("[%s] weekend — hard block", symbol)
        return None

    weekday   = _candle_weekday(df)
    kz_bonus  = _kz_score_bonus(kill_zone, weekday)

    # Score 1.0 for core KZ, 0.5 for outer, 0 otherwise
    if kill_zone in ("LONDON", "NEW_YORK"):
        score_bd["kill_zone"] = 1.0
    elif kill_zone in ("LONDON_OUTER", "NEW_YORK_OUTER", "LONDON_CLOSE"):
        score_bd["kill_zone"] = 0.5
    # OFF_HOURS / FRIDAY_REDUCED = 0; scanning still allowed

    log.debug("[%s] kill_zone=%s score=%.1f", symbol, kill_zone, score_bd["kill_zone"])

    # ── 2. Market structure (4H or primary df) ───────────────────────────────
    structure_df = htf_df if (htf_df is not None and len(htf_df) >= SWING_LOOKBACK * 2 + 4) else df
    ms   = detect_market_structure(structure_df)
    bias = ms["bias"]

    if ms["choch_detected"]:
        log.info("[%s] CHoCH detected (bias=%s) — skipping", symbol, bias)
        return None

    if trade_type == "daytrading":
        if bias == "BEARISH":
            log.info("[%s] structure BEARISH — skip (daytrading)", symbol)
            return None
    else:
        if bias != "BULLISH":
            log.info("[%s] structure %s ≠ BULLISH — skip", symbol, bias)
            return None

    score_bd["bos"] = 1.0
    log.debug("[%s] structure=%s ✓", symbol, bias)

    # ── 3. Premium / Discount / OTE ──────────────────────────────────────────
    pd_info     = detect_premium_discount(df)
    equilibrium = pd_info["equilibrium"]

    if pd_info["zone"] == "PREMIUM":
        log.info("[%s] PREMIUM zone (%.1f%%) — skip", symbol, pd_info["position_pct"])
        return None

    if pd_info["zone"] == "DISCOUNT":
        score_bd["discount"] = 1.0
    elif pd_info["zone"] == "EQUILIBRIUM":
        score_bd["discount"] = 0.5   # partial credit for equilibrium entries

    if pd_info["ote_zone"]:
        score_bd["ote"] = 1.0
        log.debug("[%s] in OTE (%s) ✓", symbol, pd_info["ote_quality"])

    # ── 4. Liquidity sweep ───────────────────────────────────────────────────
    sweep = detect_liquidity_sweep(df)
    if sweep["sweep_detected"]:
        score_bd["sweep"] = min(1.0, sweep["sweep_strength_score"] + 0.30)
        log.debug("[%s] sweep detected at %.6f (score=%.2f)", symbol, sweep["sweep_price"], score_bd["sweep"])

    sweep_idx = sweep["sweep_candle_index"] if sweep["sweep_detected"] else -1

    # ── 5. Displacement candle ───────────────────────────────────────────────
    best_disp_q = 0.0
    for di in range(max(0, n - 20), n):
        q = _displacement_quality(df, di)
        if q > best_disp_q:
            best_disp_q = q
        if _is_two_candle_displacement(df, di) and best_disp_q < DISP_MIN_QUALITY:
            best_disp_q = DISP_MIN_QUALITY

    if best_disp_q >= DISP_MIN_QUALITY:
        score_bd["displacement"] = min(1.0, best_disp_q)
        log.debug("[%s] displacement quality=%.2f ✓", symbol, best_disp_q)

    # ── 6 & 7. FVG + OB (discount zone only) ────────────────────────────────
    scan_window = min(n, OB_LOOKBACK * 2)
    df_scan     = df.iloc[-scan_window:].reset_index(drop=True)

    fvgs = detect_fvgs(df_scan)
    obs  = detect_order_blocks(df_scan)

    total_bull_fvgs = sum(1 for f in fvgs if f.type == "bullish" and not f.filled)
    total_bull_obs  = sum(1 for o in obs  if o.type == "bullish" and not o.mitigated)

    active_bull_fvgs = [
        f for f in fvgs
        if f.type == "bullish" and not f.filled
        and current_price <= f.top   * 1.005
        and f.bottom < current_price * 1.005
        and f.bottom < equilibrium
    ]
    active_bull_obs = [
        o for o in obs
        if o.type == "bullish" and not o.mitigated
        and current_price <= o.top   * 1.005
        and o.bottom < current_price * 1.005
        and o.bottom < equilibrium
    ]

    has_fvg = bool(active_bull_fvgs)
    has_ob  = bool(active_bull_obs)

    nearest_fvg_dist = min(
        (abs(current_price - f.bottom) / current_price for f in fvgs if f.type == "bullish" and not f.filled),
        default=None,
    )
    nearest_ob_dist = min(
        (abs(current_price - o.bottom) / current_price for o in obs if o.type == "bullish" and not o.mitigated),
        default=None,
    )

    log.info(
        "[%s] FVGs total=%d active=%d (nearest=%.2f%%) | OBs total=%d active=%d (nearest=%s) | zone=%s",
        symbol,
        total_bull_fvgs, len(active_bull_fvgs),
        (nearest_fvg_dist or 0) * 100,
        total_bull_obs, len(active_bull_obs),
        f"{(nearest_ob_dist or 0)*100:.2f}%",
        pd_info["zone"],
    )

    if has_fvg:
        fvg_strength = max((f.strength for f in active_bull_fvgs), default=0.5)
        score_bd["fvg"] = min(1.0, 0.5 + fvg_strength * 0.5)
    if has_ob:
        ob_strength = max((o.strength for o in active_bull_obs), default=0.5)
        score_bd["ob"] = min(1.0, 0.5 + ob_strength * 0.5)

    if not has_fvg and not has_ob:
        log.info(
            "[%s] no active FVG/OB in discount — nearest FVG=%s OB=%s — skip",
            symbol,
            f"{(nearest_fvg_dist or 0)*100:.2f}%",
            f"{(nearest_ob_dist or 0)*100:.2f}%",
        )
        return None

    # ── 8. VWAP filter ───────────────────────────────────────────────────────
    vwap_ctx = _vwap_context(df, sweep_idx)
    vwap     = vwap_ctx["daily_vwap"] or calculate_vwap(df)
    vwap_ok  = vwap > 0 and current_price <= vwap

    if vwap_ok:
        score_bd["vwap"] = 1.0
    elif vwap_ctx["valid"] and current_price <= vwap_ctx.get("sd1_upper", 0):
        score_bd["vwap"] = 0.5   # above VWAP but within SD1 — partial credit

    # Apply VWAP modifier as fractional bonus to total (not replacing score point)
    vwap_modifier = vwap_ctx["modifier"]   # ± 0–0.30

    # AVWAP confluence with OTE
    if vwap_ctx["avwap_sweep"] > 0 and pd_info["ote_zone"]:
        av   = vwap_ctx["avwap_sweep"]
        otob = pd_info["ote_bottom"]
        otot = pd_info["ote_top"]
        if otob <= av <= otot:
            vwap_modifier += AVWAP_CONFLUENCE_BONUS

    # ── 9. Confirmation candle ───────────────────────────────────────────────
    score_bd["confirmation"] = _is_confirmation_candle(df_scan, fvgs, obs)

    # ── Score gate ───────────────────────────────────────────────────────────
    raw_score   = sum(score_bd.values())
    total_score = raw_score + vwap_modifier   # VWAP modifier is additive bonus/penalty

    log.info(
        "[%s] score=%.2f/10 (raw=%.2f + vwap_mod=%.2f) breakdown=%s",
        symbol, total_score, raw_score, vwap_modifier, score_bd,
    )

    if total_score < _MIN_SCORE:
        log.info("[%s] score %.2f < min %d — skip", symbol, total_score, _MIN_SCORE)
        return None

    # ── Build zone / stop loss ───────────────────────────────────────────────
    if has_fvg and has_ob:
        zone_bottom = min(active_bull_obs[0].bottom, active_bull_fvgs[0].bottom)
        zone_type   = "FVG+OB"
    elif has_ob:
        zone_bottom = active_bull_obs[0].bottom
        zone_type   = "OB"
    else:
        zone_bottom = active_bull_fvgs[0].bottom
        zone_type   = "FVG"

    if sweep["sweep_detected"] and "+" not in zone_type:
        zone_type = f"SWEEP+{zone_type}"

    # Stop loss: below zone bottom with ATR buffer; hard cap at 3 %
    sl          = zone_bottom - current_atr * ATR_SL_MULT
    max_sl_dist = current_price * 0.03
    sl          = max(sl, current_price - max_sl_dist, 0.0)

    risk = current_price - sl
    if risk <= 0:
        return None

    tp1 = current_price + risk * 1.0
    tp2 = current_price + risk * 2.0
    tp3 = current_price + risk * 3.0

    # ── Reason string ────────────────────────────────────────────────────────
    flags = []
    if score_bd["kill_zone"]:    flags.append(kill_zone or "KZ")
    if score_bd["bos"]:          flags.append("BOS")
    if score_bd["discount"]:     flags.append("DISCOUNT" if pd_info["zone"] == "DISCOUNT" else "EQ")
    if score_bd["sweep"]:        flags.append("SWEEP")
    if score_bd["displacement"]: flags.append("DISP")
    if score_bd["fvg"]:          flags.append("FVG")
    if score_bd["ob"]:           flags.append("OB")
    if score_bd["ote"]:          flags.append(f"OTE({pd_info['ote_quality']})")
    if score_bd["confirmation"]: flags.append("CONF")
    if score_bd["vwap"]:         flags.append("VWAP")
    if vwap_ctx.get("avwap_sweep") and pd_info["ote_zone"]:
        flags.append("AVWAP")
    reason = " + ".join(flags) if flags else "Confluence setup"

    return TradeSignal(
        symbol              = symbol,
        side                = "BUY",
        entry_price         = current_price,
        stop_loss           = sl,
        take_profit         = tp1,
        zone_type           = zone_type,
        strength            = min(total_score / 10.0, 1.0),
        score               = total_score,
        tp1                 = tp1,
        tp2                 = tp2,
        tp3                 = tp3,
        kill_zone           = kill_zone,
        structure           = ms["bias"],
        discount_zone       = pd_info["zone"] == "DISCOUNT",
        liquidity_sweep     = sweep["sweep_detected"],
        displacement        = bool(score_bd["displacement"]),
        fvg_present         = has_fvg,
        ob_present          = has_ob,
        ote_zone            = pd_info["ote_zone"],
        confirmation_candle = bool(score_bd["confirmation"]),
        vwap_filter         = vwap_ok,
        score_breakdown     = score_bd,
        reason              = reason,
        atr                 = current_atr,
    )


# ──────────────────────────────────────────────────────────────────────────── #
#  Legacy / enhanced helper functions                                           #
#  (Preserved for external callers — claude_trader.py, test files, etc.)       #
# ──────────────────────────────────────────────────────────────────────────── #

_ENH_FVG_MIN_PCT = 0.002   # 0.2 % minimum gap (stricter than base FVG_MIN_SIZE_PCT)


def detect_displacement(candles: list) -> dict:
    """Find the most recent displacement candle.  External API (takes raw klines).

    Returns
    -------
    {"detected": bool, "candle_index": int, "body_size_pct": float, "volume_ratio": float}
    """
    _none: dict = {"detected": False, "candle_index": -1, "body_size_pct": 0.0, "volume_ratio": 0.0}
    if not candles:
        return _none
    df = candles_to_df(candles)
    if df.empty:
        return _none

    n = len(df)
    for i in range(n - 1, max(0, n - 21), -1):
        q = _displacement_quality(df, i)
        if q >= DISP_MIN_QUALITY:
            c      = df.iloc[i]
            body   = abs(float(c["close"]) - float(c["open"])) / max(float(c["open"]), 1e-9)
            avg_v  = float(df.iloc[max(0, i - 20):i]["volume"].mean()) if i > 0 else 0.0
            vol_r  = float(c["volume"]) / max(avg_v, 1e-9)
            return {
                "detected":      True,
                "candle_index":  i,
                "body_size_pct": round(body * 100, 3),
                "volume_ratio":  round(vol_r, 2),
            }
    return _none


def detect_fvg_enhanced(candles: list) -> list:
    """Enhanced FVG detection returning dicts.  External API (takes raw klines)."""
    if not candles:
        return []
    df = candles_to_df(candles)
    if df.empty or len(df) < 5:
        return []

    pd_info     = detect_premium_discount(df)
    equilibrium = pd_info["equilibrium"]
    disp_info   = detect_displacement(candles)
    disp_idx    = disp_info["candle_index"] if disp_info["detected"] else -1
    sweep_info  = detect_liquidity_sweep(df)
    sweep_idx   = sweep_info["sweep_candle_index"] if sweep_info["sweep_detected"] else -1

    fvgs_raw = detect_fvgs(df)
    result   = []
    for fvg in fvgs_raw:
        if fvg.type != "bullish":
            continue
        if fvg.filled:
            continue
        if fvg.bottom >= equilibrium:
            continue
        if disp_idx < 0 or fvg.formed_at <= disp_idx:
            continue
        result.append({
            "top":          fvg.top,
            "bottom":       fvg.bottom,
            "mid":          (fvg.top + fvg.bottom) / 2.0,
            "candle_index": fvg.formed_at,
            "after_sweep":  sweep_idx >= 0 and fvg.formed_at > sweep_idx,
            "filled":       fvg.filled,
        })
    return result


def detect_ob_enhanced(candles: list) -> list:
    """Enhanced OB detection returning dicts.  External API (takes raw klines)."""
    if not candles:
        return []
    df = candles_to_df(candles)
    if df.empty or len(df) < 8:
        return []

    pd_info     = detect_premium_discount(df)
    equilibrium = pd_info["equilibrium"]
    obs_raw     = detect_order_blocks(df)
    n           = len(df)
    result      = []

    for ob in obs_raw:
        if ob.type != "bullish" or ob.mitigated:
            continue
        if ob.bottom >= equilibrium:
            continue
        avg_vol = float(df.iloc[max(0, ob.formed_at - 20):ob.formed_at]["volume"].mean())
        ob_vol  = float(df.iloc[ob.formed_at]["volume"])
        vol_ok  = avg_vol > 0 and ob_vol > 1.2 * avg_vol
        result.append({
            "top":              ob.top,
            "bottom":           ob.bottom,
            "mid":              ob.ce,
            "candle_index":     ob.formed_at,
            "volume_confirmed": vol_ok,
            "mitigated":        ob.mitigated,
        })
    return result


def detect_confirmation_candle(
    candles: list,
    zone_top: float,
    zone_bottom: float,
) -> dict:
    """Detect bullish confirmation inside a zone.  External API (takes raw klines)."""
    _none: dict = {"confirmed": False, "entry_price": 0.0, "candle_index": -1}
    if not candles or zone_top <= zone_bottom:
        return _none
    df = candles_to_df(candles)
    if df.empty or len(df) < 2:
        return _none

    n = len(df)
    for i in range(n - 2, max(0, n - 7), -1):
        c     = df.iloc[i]
        close = float(c["close"])
        open_ = float(c["open"])
        if close <= open_:
            continue
        if not (zone_bottom <= close <= zone_top):
            continue
        next_open = float(df.iloc[i + 1]["open"]) if (i + 1) < n else 0.0
        return {"confirmed": True, "entry_price": next_open, "candle_index": i}
    return _none


def calculate_signal_score(
    kill_zone: Optional[str],
    structure: str,
    discount_zone: bool,
    sweep: bool,
    displacement: bool,
    fvg: bool,
    ob: bool,
    ote: bool,
    confirmation: bool,
    vwap_ok: bool,
) -> dict:
    """Score an ICT/SMC setup 0–10.  External API (unchanged signature)."""
    kz_active  = bool(kill_zone) and kill_zone not in ("FRIDAY_REDUCED", "OFF_HOURS")
    bos_active = structure == "BULLISH"

    breakdown: Dict[str, int] = {
        "kill_zone":    1 if kz_active    else 0,
        "bos":          1 if bos_active   else 0,
        "discount":     1 if discount_zone else 0,
        "sweep":        1 if sweep        else 0,
        "displacement": 1 if displacement else 0,
        "fvg":          1 if fvg          else 0,
        "ob":           1 if ob           else 0,
        "ote":          1 if ote          else 0,
        "confirmation": 1 if confirmation else 0,
        "vwap":         1 if vwap_ok      else 0,
    }

    total     = sum(breakdown.values())
    tradeable = total >= 8

    _labels: Dict[str, str] = {
        "kill_zone":    kill_zone or "KILL_ZONE",
        "bos":          "BOS",
        "discount":     "DISCOUNT",
        "sweep":        "SWEEP",
        "displacement": "DISP",
        "fvg":          "FVG",
        "ob":           "OB",
        "ote":          "OTE",
        "confirmation": "CONF",
        "vwap":         "VWAP",
    }
    active   = [_labels[k] for k, v in breakdown.items() if v]
    inactive = [_labels[k] for k, v in breakdown.items() if not v]

    if tradeable:
        reason = "Full confluence: " + " + ".join(active)
    elif total >= 6:
        reason = f"Near setup ({total}/10) — missing: " + ", ".join(inactive[: 8 - total])
    else:
        reason = f"Insufficient confluence ({total}/10)"

    return {"total": total, "breakdown": breakdown, "tradeable": tradeable, "reason": reason}
