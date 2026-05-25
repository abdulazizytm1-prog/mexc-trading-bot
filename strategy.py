"""
SMC + ICT strategy — spot long entries only.

Full ICT/SMC confluence engine:
  - Kill Zones (London Open / NY Open / London Close)
  - Market Structure (BOS / CHoCH on 4H)
  - Premium / Discount / OTE zones
  - Liquidity Sweep detection
  - Displacement candle confirmation
  - Fair Value Gaps (enhanced — discount zone only, post-sweep)
  - Order Blocks (enhanced — discount zone only, volume-confirmed)
  - VWAP session filter
  - 10-point scoring system (minimum 8 to generate a signal)

Existing detection primitives (FVG, OB, candle utilities) are unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import (
    ATR_PERIOD,
    ATR_SL_MULT,
    FVG_MIN_SIZE_PCT,
    OB_LOOKBACK,
    OB_MIN_IMPULSE_PCT,
    TAKE_PROFIT_RR,
)

log = logging.getLogger(__name__)

# Minimum confluence score to emit a BUY signal (out of 10)
_MIN_SCORE = 8


# ------------------------------------------------------------------ #
#  Data structures                                                     #
# ------------------------------------------------------------------ #

@dataclass
class FairValueGap:
    type: str        # "bullish" | "bearish"
    top: float
    bottom: float
    formed_at: int   # candle index where gap was confirmed
    filled: bool = False


@dataclass
class OrderBlock:
    type: str        # "bullish" | "bearish"
    top: float
    bottom: float
    formed_at: int   # candle index of the OB candle itself
    mitigated: bool = False


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
    score:              int   = 0
    tp1:                float = 0.0   # 1:1 RR — close 33%
    tp2:                float = 0.0   # 2:1 RR — close 33%
    tp3:                float = 0.0   # 3:1 RR — close 34%
    kill_zone:          Optional[str] = None   # "LONDON" | "NEW_YORK" | "LONDON_CLOSE" | "FRIDAY_REDUCED"
    structure:          str  = "NEUTRAL"       # "BULLISH" | "BEARISH" | "NEUTRAL"
    discount_zone:      bool = False
    liquidity_sweep:    bool = False
    displacement:       bool = False
    fvg_present:        bool = False
    ob_present:         bool = False
    ote_zone:           bool = False
    confirmation_candle: bool = False
    vwap_filter:        bool = False
    score_breakdown:    Dict[str, int] = field(default_factory=dict)
    reason:             str  = ""
    atr:                float = 0.0


# ------------------------------------------------------------------ #
#  Candle utilities  (UNCHANGED)                                       #
# ------------------------------------------------------------------ #

# MEXC v3 kline response: exactly 8 fields per row.
# [open_time, open, high, low, close, volume, close_time, quote_volume]
_MEXC_KLINE_COLS = [
    "open_time", "open", "high", "low", "close",
    "volume", "close_time", "quote_volume",
]
_MEXC_KLINE_NCOLS = len(_MEXC_KLINE_COLS)  # 8


def candles_to_df(raw_klines: list) -> pd.DataFrame:
    """
    Convert a raw MEXC kline list to a typed DataFrame.

    MEXC v3 returns exactly 8 fields per candle:
        [open_time, open, high, low, close, volume, close_time, quote_volume]

    If the actual row width differs from the expected 8 (API version change),
    we truncate or pad with None so pandas never raises a column-count error.
    """
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
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift(1)).abs()
    lc = (df["low"]  - df["close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# ------------------------------------------------------------------ #
#  FVG detection  (UNCHANGED)                                          #
# ------------------------------------------------------------------ #

def detect_fvgs(df: pd.DataFrame) -> List[FairValueGap]:
    """
    Identify all Fair Value Gaps in the candle series.

    Bullish FVG  : candle[i-2].high < candle[i].low   — upward gap
    Bearish FVG  : candle[i-2].low  > candle[i].high  — downward gap

    A gap is only recorded when its size is at least FVG_MIN_SIZE_PCT
    of the mid-price, filtering out noise.
    """
    fvgs: List[FairValueGap] = []
    n = len(df)

    for i in range(2, n):
        c_prev2 = df.iloc[i - 2]
        c_curr  = df.iloc[i]

        if c_curr["low"] > c_prev2["high"]:
            gap = c_curr["low"] - c_prev2["high"]
            mid = (c_curr["low"] + c_prev2["high"]) / 2
            if gap / mid >= FVG_MIN_SIZE_PCT:
                fvgs.append(FairValueGap(
                    type="bullish", top=c_curr["low"],
                    bottom=c_prev2["high"], formed_at=i,
                ))

        elif c_curr["high"] < c_prev2["low"]:
            gap = c_prev2["low"] - c_curr["high"]
            mid = (c_prev2["low"] + c_curr["high"]) / 2
            if gap / mid >= FVG_MIN_SIZE_PCT:
                fvgs.append(FairValueGap(
                    type="bearish", top=c_prev2["low"],
                    bottom=c_curr["high"], formed_at=i,
                ))

    for fvg in fvgs:
        for j in range(fvg.formed_at + 1, n):
            c = df.iloc[j]
            if fvg.type == "bullish" and c["low"] <= fvg.bottom:
                fvg.filled = True
                break
            if fvg.type == "bearish" and c["high"] >= fvg.top:
                fvg.filled = True
                break

    return fvgs


# ------------------------------------------------------------------ #
#  Order Block detection  (UNCHANGED)                                  #
# ------------------------------------------------------------------ #

def detect_order_blocks(df: pd.DataFrame) -> List[OrderBlock]:
    """
    Identify Order Blocks: the last opposing candle before a confirmed
    impulse move of at least OB_MIN_IMPULSE_PCT.

    Bullish OB : last bearish candle → followed by bullish impulse
    Bearish OB : last bullish candle → followed by bearish impulse

    The impulse is measured across the 3 candles following the OB candle.
    """
    obs: List[OrderBlock] = []
    n = len(df)
    lookahead = 3

    for i in range(1, n - lookahead):
        c = df.iloc[i]
        is_bearish = c["close"] < c["open"]
        is_bullish = c["close"] > c["open"]
        future_close = df.iloc[i + lookahead]["close"]

        if is_bearish:
            impulse = (future_close - c["close"]) / c["close"]
            if impulse >= OB_MIN_IMPULSE_PCT:
                if all(df.iloc[j]["close"] >= df.iloc[j]["open"]
                       for j in range(i + 1, i + lookahead)):
                    obs.append(OrderBlock(
                        type="bullish",
                        top=max(c["open"], c["close"]),
                        bottom=c["low"],
                        formed_at=i,
                    ))

        elif is_bullish:
            impulse = (c["close"] - future_close) / c["close"]
            if impulse >= OB_MIN_IMPULSE_PCT:
                if all(df.iloc[j]["close"] <= df.iloc[j]["open"]
                       for j in range(i + 1, i + lookahead)):
                    obs.append(OrderBlock(
                        type="bearish",
                        top=c["high"],
                        bottom=min(c["open"], c["close"]),
                        formed_at=i,
                    ))

    for ob in obs:
        for j in range(ob.formed_at + lookahead + 1, n):
            c = df.iloc[j]
            if ob.type == "bullish" and c["low"] <= ob.bottom:
                ob.mitigated = True
                break
            if ob.type == "bearish" and c["high"] >= ob.top:
                ob.mitigated = True
                break

    return obs


# ------------------------------------------------------------------ #
#  Recency score  (UNCHANGED)                                          #
# ------------------------------------------------------------------ #

def _recency_score(formed_at: int, total_candles: int) -> float:
    """Older zones get lower scores; zones formed in last 20 candles score ~1."""
    age = total_candles - formed_at
    return max(0.0, 1.0 - age / max(total_candles, 1))


# ================================================================== #
#  ICT / SMC analysis functions                                        #
# ================================================================== #

# ------------------------------------------------------------------ #
#  1. Kill Zone                                                        #
# ------------------------------------------------------------------ #

def detect_kill_zone(dt: Optional[datetime] = None) -> Optional[str]:
    """
    Returns the active ICT Kill Zone name, or None if outside trading hours.

    Active kill zones (UTC):
      London Open   07:00 – 10:00  Tue–Thu (full size)
      New York Open 13:00 – 16:00  Tue–Thu (full size)
      London Close  15:00 – 17:00  Tue–Thu (full size; overlaps NY 15–16)

    Special days:
      Monday  → None        (accumulation day — no new entries)
      Friday  → "FRIDAY_REDUCED" if in kill zone hours (half position size)
      Sat/Sun → None        (weekend — no trade)

    Returns
    -------
    "LONDON" | "NEW_YORK" | "LONDON_CLOSE" | "FRIDAY_REDUCED" | None
    """
    now = dt or datetime.now(timezone.utc)
    weekday = now.weekday()   # 0 = Monday … 6 = Sunday
    hour    = now.hour

    # Weekend
    if weekday >= 5:
        return None

    # Monday — accumulation, skip
    if weekday == 0:
        return None

    in_london       = 7  <= hour < 10
    in_ny           = 13 <= hour < 16
    in_london_close = 15 <= hour < 17

    # Friday — only reduced-size entries during active hours
    if weekday == 4:
        if in_london or in_ny or in_london_close:
            return "FRIDAY_REDUCED"
        return None

    # Tuesday – Thursday: full sessions
    # London Close takes priority over NY in the 15:00-15:59 overlap
    if in_london_close:
        return "LONDON_CLOSE"
    if in_ny:
        return "NEW_YORK"
    if in_london:
        return "LONDON"

    return None


# ------------------------------------------------------------------ #
#  Internal pivot helpers                                              #
# ------------------------------------------------------------------ #

def _find_swing_highs(
    df: pd.DataFrame, strength: int = 2
) -> List[Tuple[int, float]]:
    """Pivot highs: candle whose high is strictly greater than `strength` neighbours on each side."""
    pivots: List[Tuple[int, float]] = []
    n = len(df)
    for i in range(strength, n - strength):
        hi = float(df.iloc[i]["high"])
        neighbours = [
            float(df.iloc[j]["high"])
            for j in range(i - strength, i + strength + 1)
            if j != i
        ]
        if all(hi > v for v in neighbours):
            pivots.append((i, hi))
    return pivots


def _find_swing_lows(
    df: pd.DataFrame, strength: int = 2
) -> List[Tuple[int, float]]:
    """Pivot lows: candle whose low is strictly less than `strength` neighbours on each side."""
    pivots: List[Tuple[int, float]] = []
    n = len(df)
    for i in range(strength, n - strength):
        lo = float(df.iloc[i]["low"])
        neighbours = [
            float(df.iloc[j]["low"])
            for j in range(i - strength, i + strength + 1)
            if j != i
        ]
        if all(lo < v for v in neighbours):
            pivots.append((i, lo))
    return pivots


# ------------------------------------------------------------------ #
#  2. Market Structure (BOS / CHoCH)                                   #
# ------------------------------------------------------------------ #

def detect_market_structure(df: pd.DataFrame) -> dict:
    """
    Detect higher-timeframe market structure bias.

    Parameters
    ----------
    df : 4H (or primary) candle DataFrame — last ~40 candles recommended.

    Algorithm
    ---------
    1. Find all pivot highs and pivot lows (strength = 2).
    2. BOS (Break of Structure): current close > last swing high → BULLISH.
    3. Sequence check: two consecutive HH+HL → BULLISH; LH+LL → BEARISH.
    4. CHoCH (Change of Character): after a BULLISH sequence, a lower high
       forms (swing_high[-1] < swing_high[-2]) → reversal warning.

    Returns
    -------
    {
      "bias":          "BULLISH" | "BEARISH" | "NEUTRAL",
      "last_bos":      float,   # price level of the broken swing high (0 if none)
      "choch_detected": bool,
      "swing_high":    float,   # most recent pivot high
      "swing_low":     float,   # most recent pivot low
    }
    """
    _default = {
        "bias": "NEUTRAL", "last_bos": 0.0,
        "choch_detected": False, "swing_high": 0.0, "swing_low": 0.0,
    }
    if len(df) < 10:
        return _default

    highs = _find_swing_highs(df, strength=2)
    lows  = _find_swing_lows(df, strength=2)

    current = float(df.iloc[-1]["close"])
    bias          = "NEUTRAL"
    last_bos      = 0.0
    choch_detected = False

    swing_high = highs[-1][1] if highs else 0.0
    swing_low  = lows[-1][1]  if lows  else 0.0

    # ── BOS: close breaks above the most recent swing high ──────────────────
    if highs and current > highs[-1][1]:
        bias     = "BULLISH"
        last_bos = highs[-1][1]

    # ── Sequence analysis: need at least two pivot highs and two pivot lows ─
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]   # higher high
        hl = lows[-1][1]  > lows[-2][1]    # higher low

        if hh and hl:
            bias = "BULLISH"
        elif not hh and not hl:
            bias = "BEARISH"

        # CHoCH: previous sequence was bullish (HH) but latest high is lower
        if not hh and lows[-1][1] > lows[-2][1]:
            # Higher lows still, but lower high — character is changing
            choch_detected = True

    # If a BOS just fired but CHoCH is also present, CHoCH wins (abort trade)
    return {
        "bias":           bias,
        "last_bos":       last_bos,
        "choch_detected": choch_detected,
        "swing_high":     swing_high,
        "swing_low":      swing_low,
    }


# ------------------------------------------------------------------ #
#  3. Premium / Discount / OTE zones                                   #
# ------------------------------------------------------------------ #

def detect_premium_discount(df: pd.DataFrame) -> dict:
    """
    Classify current price relative to the recent trading range.

    Uses the last 40 candles (or all available).

    Zones (measured from range low, 0 % = range_low, 100 % = range_high):
      Discount    < 50 %    — preferred buy area
      Equilibrium 50 – 75 % — neutral
      Premium     > 75 %    — avoid buying

    OTE (Optimal Trade Entry): price is at 62 – 79 % of the range,
    consistent with a 61.8 – 79 % Fibonacci retracement target after a
    displacement move.

    Returns
    -------
    {
      "zone":         "DISCOUNT" | "EQUILIBRIUM" | "PREMIUM",
      "ote_zone":     bool,
      "equilibrium":  float,
      "range_high":   float,
      "range_low":    float,
      "position_pct": float,   # 0–100
    }
    """
    recent = df.iloc[-40:] if len(df) >= 40 else df
    range_high = float(recent["high"].max())
    range_low  = float(recent["low"].min())
    current    = float(df.iloc[-1]["close"])

    _eq = (range_high + range_low) / 2.0
    if range_high <= range_low:
        return {
            "zone": "EQUILIBRIUM", "ote_zone": False,
            "equilibrium": _eq, "range_high": range_high,
            "range_low": range_low, "position_pct": 50.0,
        }

    position_pct = (current - range_low) / (range_high - range_low) * 100.0

    if position_pct < 50.0:
        zone = "DISCOUNT"
    elif position_pct > 75.0:
        zone = "PREMIUM"
    else:
        zone = "EQUILIBRIUM"

    ote_zone = 62.0 <= position_pct <= 79.0

    return {
        "zone":         zone,
        "ote_zone":     ote_zone,
        "equilibrium":  _eq,
        "range_high":   range_high,
        "range_low":    range_low,
        "position_pct": round(position_pct, 1),
    }


# ------------------------------------------------------------------ #
#  4. Liquidity Sweep                                                  #
# ------------------------------------------------------------------ #

def detect_liquidity_sweep(df: pd.DataFrame) -> dict:
    """
    Detect a buy-side liquidity sweep below a recent swing low.

    Sweep is confirmed when, in the last 20 candles:
      1. A candle's wick (low) dips at least 0.3 % below a swing low.
      2. That candle's body (close) closes back above the swing low.

    The most recent qualifying sweep is returned.

    Returns
    -------
    {
      "sweep_detected":     bool,
      "sweep_price":        float,  # the swing low that was swept
      "sweep_candle_index": int,    # index in the 20-candle slice (-1 if none)
      "sweep_strength":     float,  # how far below swing low the wick went (%)
    }
    """
    _no = {"sweep_detected": False, "sweep_price": 0.0,
           "sweep_candle_index": -1, "sweep_strength": 0.0}

    recent = df.iloc[-20:].reset_index(drop=True) if len(df) >= 20 else df.reset_index(drop=True)
    n = len(recent)
    if n < 6:
        return _no

    swing_lows = _find_swing_lows(recent, strength=2)
    if not swing_lows:
        return _no

    # Search each candle for a sweep of any of the last 3 swing lows
    candidate_lows = swing_lows[-3:]

    # Iterate candles from newest to oldest so we return the most recent sweep
    for i in range(n - 1, -1, -1):
        candle = recent.iloc[i]
        lo    = float(candle["low"])
        close = float(candle["close"])

        for _idx, swing_lo in candidate_lows:
            if i <= _idx:
                continue   # only look at candles AFTER the swing low formed
            min_wick = swing_lo * (1.0 - 0.003)   # 0.3 % below swing low
            if lo <= min_wick and close > swing_lo:
                strength_pct = (swing_lo - lo) / swing_lo * 100.0
                return {
                    "sweep_detected":     True,
                    "sweep_price":        swing_lo,
                    "sweep_candle_index": i,
                    "sweep_strength":     round(strength_pct, 3),
                }

    return _no


# ------------------------------------------------------------------ #
#  5. VWAP                                                             #
# ------------------------------------------------------------------ #

def calculate_vwap(df: pd.DataFrame) -> float:
    """
    Volume-Weighted Average Price from the nearest session open.

    If the DataFrame has a timezone-aware 'open_time' column the function
    filters candles from the London open (07:00 UTC) or New York open
    (13:00 UTC) depending on the current UTC hour.  Falls back to all
    available candles when timestamps are unavailable.

    Formula: VWAP = Σ(typical_price × volume) / Σ(volume)
             typical_price = (high + low + close) / 3

    Returns 0.0 if no volume data is present.
    """
    if df.empty:
        return 0.0

    session_df = df

    if "open_time" in df.columns and pd.api.types.is_datetime64_any_dtype(df["open_time"]):
        now  = datetime.now(timezone.utc)
        hour = now.hour

        if 7 <= hour < 13:
            session_hour = 7    # London session
        elif hour >= 13:
            session_hour = 13   # New York session
        else:
            session_hour = 0    # Use full day

        today = now.date()
        session_start = pd.Timestamp(
            year=today.year, month=today.month, day=today.day,
            hour=session_hour, tzinfo=timezone.utc,
        )

        # open_time may be UTC-naive after pd.to_datetime(unit="ms")
        col = df["open_time"]
        if col.dt.tz is None:
            col = col.dt.tz_localize("UTC")

        mask = col >= session_start
        if mask.sum() >= 3:
            session_df = df[mask]

    typical = (session_df["high"] + session_df["low"] + session_df["close"]) / 3.0
    vol     = session_df["volume"].fillna(0)
    total_v = float(vol.sum())

    if total_v <= 0:
        return float(typical.iloc[-1]) if not typical.empty else 0.0

    return float((typical * vol).sum() / total_v)


# ================================================================== #
#  Internal helpers for displacement / confirmation                    #
# ================================================================== #

def _is_displacement_candle(df: pd.DataFrame, idx: int) -> bool:
    """
    True when candle at `idx` is a strong bullish displacement candle:
      - Bullish (close > open)
      - Body ≥ 0.5 % of close price
      - Upper wick ≤ 20 % of candle range (closes near high)
      - Volume ≥ 1.5 × 20-candle average volume
    """
    if idx < 0 or idx >= len(df):
        return False

    c     = df.iloc[idx]
    open_ = float(c["open"])
    close = float(c["close"])
    high  = float(c["high"])
    low   = float(c["low"])

    if close <= open_:
        return False   # must be bullish

    body        = close - open_
    candle_range = high - low
    if candle_range <= 0:
        return False

    body_pct    = body / close
    upper_wick  = high - close
    wick_ratio  = upper_wick / candle_range

    if body_pct < 0.005:      # body < 0.5 %
        return False
    if wick_ratio > 0.20:     # wick > 20 % of range
        return False

    # Volume check
    start      = max(0, idx - 20)
    avg_vol    = float(df.iloc[start:idx]["volume"].mean()) if idx > start else 0.0
    candle_vol = float(c["volume"])
    if avg_vol > 0 and candle_vol < 1.5 * avg_vol:
        return False

    return True


def _is_confirmation_candle(
    df: pd.DataFrame,
    fvgs: List[FairValueGap],
    obs: List[OrderBlock],
) -> bool:
    """
    True when the last candle in `df` is a bullish candle whose close is
    inside at least one active bullish FVG or Order Block.
    """
    if len(df) < 1:
        return False

    c     = df.iloc[-1]
    close = float(c["close"])
    open_ = float(c["open"])

    if close <= open_:
        return False   # must be bullish

    in_fvg = any(
        not f.filled and f.type == "bullish" and f.bottom <= close <= f.top
        for f in fvgs
    )
    in_ob = any(
        not o.mitigated and o.type == "bullish" and o.bottom <= close <= o.top
        for o in obs
    )
    return in_fvg or in_ob


# ================================================================== #
#  Signal generation  (ICT/SMC 10-point scoring)                      #
# ================================================================== #

def generate_signal(
    symbol: str,
    df: pd.DataFrame,
    htf_df: Optional[pd.DataFrame] = None,
) -> Optional[TradeSignal]:
    """
    Full ICT + SMC confluence check.  Returns a TradeSignal when score ≥ 8/10,
    or None when conditions are not met.

    Parameters
    ----------
    symbol : MEXC trading pair, e.g. "BTCUSDT"
    df     : Primary timeframe (1H) candle DataFrame
    htf_df : Higher timeframe (4H) DataFrame for structure.
             Falls back to `df` when None.

    Scoring (0 – 10, minimum 8 required)
    -------------------------------------
    +1  Kill zone (London / NY open, Tue–Thu)
    +1  4H bullish BOS confirmed
    +1  Price in discount zone (below 50 % of range)
    +1  Liquidity sweep confirmed
    +1  Displacement candle (strong body, high volume, close near high)
    +1  Bullish FVG in discount zone
    +1  OB confluence with FVG
    +1  OTE zone (62 – 79 % Fibonacci retracement)
    +1  Confirmation candle (bullish close inside FVG / OB)
    +1  VWAP filter (price at or below session VWAP)
    """
    if len(df) < max(ATR_PERIOD + 5, OB_LOOKBACK + 5):
        return None

    current_price = float(df.iloc[-1]["close"])
    current_atr   = float(_atr(df).iloc[-1])
    n             = len(df)

    score_bd: Dict[str, int] = {
        "kill_zone": 0, "bos": 0, "discount": 0,
        "sweep": 0, "displacement": 0, "fvg": 0,
        "ob": 0, "ote": 0, "confirmation": 0, "vwap": 0,
    }

    # ── 1. Kill zone ─────────────────────────────────────────────────────────
    kill_zone = detect_kill_zone()
    if kill_zone is None:
        return None                          # weekend / Monday / off-hours hard block
    if kill_zone != "FRIDAY_REDUCED":
        score_bd["kill_zone"] = 1
    # FRIDAY_REDUCED: allowed but no +1 score, and position size halved upstream

    # ── 2. Market structure (4H bias) ────────────────────────────────────────
    structure_df = htf_df if (htf_df is not None and len(htf_df) >= 10) else df
    ms = detect_market_structure(structure_df)

    if ms["choch_detected"]:
        log.debug("[%s] CHoCH detected — skipping", symbol)
        return None
    if ms["bias"] != "BULLISH":
        log.debug("[%s] Structure not BULLISH (%s) — skipping", symbol, ms["bias"])
        return None

    score_bd["bos"] = 1

    # ── 3. Premium / Discount / OTE ──────────────────────────────────────────
    pd_info = detect_premium_discount(df)

    if pd_info["zone"] == "PREMIUM":
        log.debug("[%s] Price in premium zone (%.1f%%) — skipping", symbol, pd_info["position_pct"])
        return None

    if pd_info["zone"] == "DISCOUNT":
        score_bd["discount"] = 1

    if pd_info["ote_zone"]:
        score_bd["ote"] = 1

    equilibrium = pd_info["equilibrium"]

    # ── 4. Liquidity sweep ───────────────────────────────────────────────────
    sweep = detect_liquidity_sweep(df)
    if sweep["sweep_detected"]:
        score_bd["sweep"] = 1

    # ── 5. Displacement candle ───────────────────────────────────────────────
    # Look for a displacement in the most recent 10 candles
    for di in range(max(0, n - 10), n):
        if _is_displacement_candle(df, di):
            score_bd["displacement"] = 1
            break

    # ── 6 & 7. FVG + OB (discount zone only) ────────────────────────────────
    scan_window = min(n, OB_LOOKBACK * 2)
    fvgs = detect_fvgs(df.iloc[-scan_window:].reset_index(drop=True))
    obs  = detect_order_blocks(df.iloc[-scan_window:].reset_index(drop=True))

    active_bull_fvgs = [
        f for f in fvgs
        if f.type == "bullish"
        and not f.filled
        and f.bottom <= current_price <= f.top
        and f.bottom < equilibrium        # must sit in discount half
    ]
    active_bull_obs = [
        o for o in obs
        if o.type == "bullish"
        and not o.mitigated
        and o.bottom <= current_price <= o.top
        and o.bottom < equilibrium
    ]

    has_fvg = bool(active_bull_fvgs)
    has_ob  = bool(active_bull_obs)

    if has_fvg:
        score_bd["fvg"] = 1
    if has_ob and has_fvg:
        score_bd["ob"] = 1   # OB only adds score when confluent with FVG

    # Must have at least one zone to enter
    if not has_fvg and not has_ob:
        log.debug("[%s] No active FVG or OB in discount zone — skipping", symbol)
        return None

    # ── 8. VWAP filter ───────────────────────────────────────────────────────
    vwap     = calculate_vwap(df)
    vwap_ok  = vwap > 0 and current_price <= vwap
    if vwap_ok:
        score_bd["vwap"] = 1

    # ── 9. Confirmation candle ───────────────────────────────────────────────
    if _is_confirmation_candle(df, fvgs, obs):
        score_bd["confirmation"] = 1

    # ── Score gate ───────────────────────────────────────────────────────────
    total_score = sum(score_bd.values())
    if total_score < _MIN_SCORE:
        log.debug(
            "[%s] Score %d/%d — below minimum %d (bd=%s)",
            symbol, total_score, 10, _MIN_SCORE, score_bd,
        )
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

    # Prefix zone type if sweep confirmed (post-sweep setup is strongest)
    if sweep["sweep_detected"] and "+" not in zone_type:
        zone_type = f"SWEEP+{zone_type}"

    # Stop loss: below zone bottom with ATR buffer; hard cap at 3 % from entry
    sl = zone_bottom - current_atr * ATR_SL_MULT
    max_sl_dist = current_price * 0.03
    sl = max(sl, current_price - max_sl_dist, 0.0)

    risk = current_price - sl
    if risk <= 0:
        return None

    # Partial take-profit targets (1:1, 2:1, 3:1)
    tp1 = current_price + risk * 1.0
    tp2 = current_price + risk * 2.0
    tp3 = current_price + risk * 3.0

    # ── Reason string ────────────────────────────────────────────────────────
    flags = []
    if score_bd["kill_zone"]:    flags.append(kill_zone or "KILL_ZONE")
    if score_bd["bos"]:          flags.append("BOS")
    if score_bd["discount"]:     flags.append("DISCOUNT")
    if score_bd["sweep"]:        flags.append("SWEEP")
    if score_bd["displacement"]: flags.append("DISP")
    if score_bd["fvg"]:          flags.append("FVG")
    if score_bd["ob"]:           flags.append("OB")
    if score_bd["ote"]:          flags.append("OTE")
    if score_bd["confirmation"]: flags.append("CONF")
    if score_bd["vwap"]:         flags.append("VWAP")
    reason = " + ".join(flags) if flags else "Confluence setup"

    return TradeSignal(
        symbol              = symbol,
        side                = "BUY",
        entry_price         = current_price,
        stop_loss           = sl,
        take_profit         = tp1,       # backward compat alias
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


# ================================================================== #
#  Enhanced signal generation functions                                #
# ================================================================== #

_ENH_FVG_MIN_PCT = 0.002   # 0.2 % minimum gap (stricter than base 0.1 %)


def detect_displacement(candles: list) -> dict:
    """
    Find the most recent displacement candle (strong institutional buying).

    A displacement candle must satisfy all four conditions:
      1. Bullish (close > open)
      2. Body ≥ 0.5 % of close price
      3. Upper wick ≤ 20 % of candle range (closes near its high)
      4. Volume ≥ 1.5 × 20-candle average volume

    Searches the last 20 candles, newest first.

    Parameters
    ----------
    candles : Raw MEXC kline list (same format accepted by candles_to_df)

    Returns
    -------
    {
      "detected":      bool,
      "candle_index":  int,    # index in the DataFrame (-1 when not found)
      "body_size_pct": float,  # body as % of close price
      "volume_ratio":  float,  # candle volume / 20-bar average volume
    }
    """
    _none: dict = {
        "detected": False, "candle_index": -1,
        "body_size_pct": 0.0, "volume_ratio": 0.0,
    }

    if not candles:
        return _none

    df = candles_to_df(candles)
    if df.empty:
        return _none

    n            = len(df)
    search_start = max(0, n - 20)

    for i in range(n - 1, search_start - 1, -1):
        c     = df.iloc[i]
        open_ = float(c["open"])
        close = float(c["close"])
        high  = float(c["high"])
        low   = float(c["low"])

        if close <= open_:
            continue

        body         = close - open_
        candle_range = high - low
        if candle_range <= 0 or close <= 0:
            continue

        body_pct   = body / close
        wick_ratio = (high - close) / candle_range

        if body_pct < 0.005 or wick_ratio > 0.20:
            continue

        vol_start = max(0, i - 20)
        avg_vol   = float(df.iloc[vol_start:i]["volume"].mean()) if i > vol_start else 0.0
        c_vol     = float(c["volume"])
        vol_ratio = (c_vol / avg_vol) if avg_vol > 0 else 0.0

        if avg_vol > 0 and c_vol < 1.5 * avg_vol:
            continue

        return {
            "detected":      True,
            "candle_index":  i,
            "body_size_pct": round(body_pct * 100.0, 3),
            "volume_ratio":  round(vol_ratio, 2),
        }

    return _none


def detect_fvg_enhanced(candles: list) -> list:
    """
    Enhanced Fair Value Gap detection.

    Extends the base FVG detector with four additional quality filters:
      - Minimum gap ≥ 0.2 % of mid-price (vs 0.1 % in base)
      - FVG bottom must be below the 40-candle equilibrium (discount zone)
      - FVG must form after a displacement candle
      - ``after_sweep`` flag marks FVGs that formed after a liquidity sweep
        (highest-quality setups per ICT methodology)

    Only bullish FVGs are returned (spot long-only strategy).

    Parameters
    ----------
    candles : Raw MEXC kline list

    Returns
    -------
    List of dicts, one per qualifying FVG (oldest → newest):
    {
      "top":          float,
      "bottom":       float,
      "mid":          float,
      "candle_index": int,
      "after_sweep":  bool,
      "filled":       bool,
    }
    """
    if not candles:
        return []

    df = candles_to_df(candles)
    if df.empty or len(df) < 5:
        return []

    pd_info     = detect_premium_discount(df)
    equilibrium = pd_info["equilibrium"]

    disp_info = detect_displacement(candles)
    disp_idx  = disp_info["candle_index"] if disp_info["detected"] else -1

    sweep_info = detect_liquidity_sweep(df)
    sweep_idx  = (
        sweep_info["sweep_candle_index"]
        if sweep_info["sweep_detected"]
        else -1
    )

    result: list = []
    n = len(df)

    for i in range(2, n):
        c0 = df.iloc[i - 2]   # candle[0] of the 3-bar pattern
        c2 = df.iloc[i]       # candle[2]

        gap_lo = float(c2["low"])
        gap_hi = float(c0["high"])

        if gap_lo <= gap_hi:
            continue   # no bullish gap

        gap      = gap_lo - gap_hi
        mid_p    = (gap_lo + gap_hi) / 2.0
        gap_pct  = gap / mid_p if mid_p > 0 else 0.0

        if gap_pct < _ENH_FVG_MIN_PCT:
            continue   # gap too small

        if gap_hi >= equilibrium:
            continue   # not in discount zone

        if disp_idx < 0 or i <= disp_idx:
            continue   # no prior displacement candle

        filled = any(float(df.iloc[j]["low"]) <= gap_hi for j in range(i + 1, n))

        result.append({
            "top":          gap_lo,
            "bottom":       gap_hi,
            "mid":          (gap_lo + gap_hi) / 2.0,
            "candle_index": i,
            "after_sweep":  sweep_idx >= 0 and i > sweep_idx,
            "filled":       filled,
        })

    return result


def detect_ob_enhanced(candles: list) -> list:
    """
    Enhanced Order Block detection.

    Extends the base OB detector with two additional quality filters:
      - Volume confirmation: OB candle volume > 1.2 × 20-bar average
      - Discount zone filter: OB bottom must be below 40-candle equilibrium

    Only bullish OBs are returned (last bearish candle before a ≥ 0.5 %
    upward impulse over the next 3 candles).

    Parameters
    ----------
    candles : Raw MEXC kline list

    Returns
    -------
    List of dicts (oldest → newest):
    {
      "top":              float,
      "bottom":           float,
      "mid":              float,
      "candle_index":     int,
      "volume_confirmed": bool,
      "mitigated":        bool,
    }
    """
    if not candles:
        return []

    df = candles_to_df(candles)
    if df.empty or len(df) < 8:
        return []

    pd_info     = detect_premium_discount(df)
    equilibrium = pd_info["equilibrium"]

    result: list = []
    n         = len(df)
    lookahead = 3

    for i in range(1, n - lookahead):
        c = df.iloc[i]

        if float(c["close"]) >= float(c["open"]):
            continue   # must be a bearish candle

        close_c       = float(c["close"])
        future_close  = float(df.iloc[i + lookahead]["close"])
        impulse       = (future_close - close_c) / close_c if close_c > 0 else 0.0

        if impulse < OB_MIN_IMPULSE_PCT:
            continue

        if not all(
            float(df.iloc[j]["close"]) >= float(df.iloc[j]["open"])
            for j in range(i + 1, i + lookahead)
        ):
            continue

        ob_bottom = float(c["low"])
        ob_top    = max(float(c["open"]), close_c)

        if ob_bottom >= equilibrium:
            continue   # not in discount zone

        vol_start     = max(0, i - 20)
        avg_vol       = float(df.iloc[vol_start:i]["volume"].mean()) if i > vol_start else 0.0
        ob_vol        = float(c["volume"])
        vol_confirmed = avg_vol > 0 and ob_vol > 1.2 * avg_vol

        mitigated = any(
            float(df.iloc[j]["low"]) <= ob_bottom
            for j in range(i + lookahead + 1, n)
        )

        result.append({
            "top":              ob_top,
            "bottom":           ob_bottom,
            "mid":              (ob_top + ob_bottom) / 2.0,
            "candle_index":     i,
            "volume_confirmed": vol_confirmed,
            "mitigated":        mitigated,
        })

    return result


def detect_ote_zone(
    swing_low: float,
    swing_high: float,
    current_price: float,
) -> dict:
    """
    Optimal Trade Entry zone via Fibonacci retracement.

    Measures how far the current price has pulled back from ``swing_high``
    toward ``swing_low`` after a bullish impulse move.

    Key levels
    ----------
    fib_618 : 61.8 % retracement — deepest acceptable OTE entry
    fib_65  : 65.0 % retracement — golden-pocket bottom
    fib_79  : 79.0 % retracement — shallowest acceptable OTE entry

    Zone quality
    ------------
    GOLDEN  — price between fib_618 and fib_65 (highest probability)
    OTE     — price between fib_79 and fib_618 (standard ICT entry)
    OUTSIDE — price outside the OTE band

    Parameters
    ----------
    swing_low     : Low of the impulse leg (start of the move)
    swing_high    : High of the impulse leg (end of the move)
    current_price : Current close price

    Returns
    -------
    {
      "in_ote":      bool,
      "fib_618":     float,
      "fib_65":      float,
      "fib_79":      float,
      "zone_quality": "GOLDEN" | "OTE" | "OUTSIDE",
    }
    """
    _outside: dict = {
        "in_ote": False, "fib_618": 0.0, "fib_65": 0.0,
        "fib_79": 0.0, "zone_quality": "OUTSIDE",
    }

    if swing_high <= swing_low or current_price <= 0:
        return _outside

    rang = swing_high - swing_low

    fib_618 = swing_high - rang * 0.618
    fib_65  = swing_high - rang * 0.650
    fib_79  = swing_high - rang * 0.790

    # fib_79 < fib_65 < fib_618 (deeper retracement = lower price)
    in_ote = fib_79 <= current_price <= fib_618

    if fib_65 <= current_price <= fib_618:
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
        "zone_quality": quality,
    }


def detect_confirmation_candle(
    candles: list,
    zone_top: float,
    zone_bottom: float,
) -> dict:
    """
    Detect a bullish confirmation candle inside a FVG or OB zone.

    Rules (ICT entry discipline):
      - The confirming candle must be bullish (close > open).
      - Its close must sit inside the zone [zone_bottom, zone_top].
      - Entry is placed at the OPEN of the candle that follows the confirming
        candle — never mid-candle.

    Searches backward from the second-to-last candle (the most recently
    completed bar) over up to 5 bars.

    Parameters
    ----------
    candles     : Raw MEXC kline list
    zone_top    : Upper boundary of the target FVG / OB zone
    zone_bottom : Lower boundary of the target FVG / OB zone

    Returns
    -------
    {
      "confirmed":    bool,
      "entry_price":  float,  # open of the candle after confirmation (0 if none)
      "candle_index": int,    # DataFrame index of the confirming candle (-1 if none)
    }
    """
    _none: dict = {"confirmed": False, "entry_price": 0.0, "candle_index": -1}

    if not candles or zone_top <= zone_bottom:
        return _none

    df = candles_to_df(candles)
    if df.empty or len(df) < 2:
        return _none

    n = len(df)

    # Search the last 5 completed candles (exclude the live forming candle at [-1])
    for i in range(n - 2, max(0, n - 7), -1):
        c     = df.iloc[i]
        close = float(c["close"])
        open_ = float(c["open"])

        if close <= open_:
            continue   # must be bullish

        if not (zone_bottom <= close <= zone_top):
            continue   # must close inside zone

        # Entry at the open of the next candle
        next_open = float(df.iloc[i + 1]["open"]) if (i + 1) < n else 0.0

        return {
            "confirmed":    True,
            "entry_price":  next_open,
            "candle_index": i,
        }

    return _none


def calculate_signal_score(
    kill_zone:     Optional[str],
    structure:     str,
    discount_zone: bool,
    sweep:         bool,
    displacement:  bool,
    fvg:           bool,
    ob:            bool,
    ote:           bool,
    confirmation:  bool,
    vwap_ok:       bool,
) -> dict:
    """
    Score an ICT/SMC confluence setup on a 0–10 scale.

    Each component contributes +1 when its condition is met.
    A total score ≥ 8 is considered a tradeable, high-confluence setup.

    Parameters
    ----------
    kill_zone     : Output of detect_kill_zone() — truthy and not "FRIDAY_REDUCED" = +1
    structure     : "BULLISH" from detect_market_structure() = +1
    discount_zone : True when price is below the 40-candle equilibrium
    sweep         : True when detect_liquidity_sweep() found a sweep
    displacement  : True when detect_displacement() found a displacement candle
    fvg           : True when detect_fvg_enhanced() has an unfilled FVG in zone
    ob            : True when detect_ob_enhanced() has an unmitigated OB in zone
    ote           : True when detect_ote_zone() returns in_ote = True
    confirmation  : True when detect_confirmation_candle() confirmed entry
    vwap_ok       : True when price is at or below session VWAP

    Returns
    -------
    {
      "total":     int,          # 0–10
      "breakdown": dict,         # component_name → 0 | 1
      "tradeable": bool,         # total >= 8
      "reason":    str,          # human-readable summary
    }
    """
    kz_active  = bool(kill_zone) and kill_zone != "FRIDAY_REDUCED"
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
        reason = (
            f"Near setup ({total}/10) — missing: "
            + ", ".join(inactive[: 8 - total])
        )
    else:
        reason = f"Insufficient confluence ({total}/10)"

    return {
        "total":     total,
        "breakdown": breakdown,
        "tradeable": tradeable,
        "reason":    reason,
    }
