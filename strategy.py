"""
SMC (Smart Money Concepts) strategy — spot long entries only.

Detects two types of institutional price zones:
  - Fair Value Gap (FVG): 3-candle imbalance where price gapped and hasn't returned.
  - Order Block (OB): Last opposing candle before a strong directional impulse.

Entry triggers when the current closing price falls inside an unfilled bullish
FVG or an unmitigated bullish OB.  Stop loss sits just below the zone;
take profit is set at a fixed risk-reward ratio defined in config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

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
    symbol: str
    side: str           # always "BUY" for spot-long strategy
    entry_price: float
    stop_loss: float
    take_profit: float
    zone_type: str      # "FVG" | "OB" | "FVG+OB" (confluence)
    strength: float     # 0.0 – 1.0


# ------------------------------------------------------------------ #
#  Candle utilities                                                    #
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
        # Truncate extra columns; pad missing ones with None
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
    lc = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# ------------------------------------------------------------------ #
#  FVG detection                                                       #
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
        c_curr = df.iloc[i]

        # --- Bullish FVG ---
        if c_curr["low"] > c_prev2["high"]:
            gap = c_curr["low"] - c_prev2["high"]
            mid = (c_curr["low"] + c_prev2["high"]) / 2
            if gap / mid >= FVG_MIN_SIZE_PCT:
                fvgs.append(FairValueGap(
                    type="bullish",
                    top=c_curr["low"],
                    bottom=c_prev2["high"],
                    formed_at=i,
                ))

        # --- Bearish FVG ---
        elif c_curr["high"] < c_prev2["low"]:
            gap = c_prev2["low"] - c_curr["high"]
            mid = (c_prev2["low"] + c_curr["high"]) / 2
            if gap / mid >= FVG_MIN_SIZE_PCT:
                fvgs.append(FairValueGap(
                    type="bearish",
                    top=c_prev2["low"],
                    bottom=c_curr["high"],
                    formed_at=i,
                ))

    # Mark gaps that have been filled by subsequent price action
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
#  Order Block detection                                               #
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
    lookahead = 3  # candles ahead to measure impulse

    for i in range(1, n - lookahead):
        c = df.iloc[i]
        is_bearish = c["close"] < c["open"]
        is_bullish = c["close"] > c["open"]

        future_close = df.iloc[i + lookahead]["close"]

        if is_bearish:
            # Potential bullish OB: measure upward impulse
            impulse = (future_close - c["close"]) / c["close"]
            if impulse >= OB_MIN_IMPULSE_PCT:
                # Confirm this is the LAST bearish candle before the move
                if all(df.iloc[j]["close"] >= df.iloc[j]["open"] for j in range(i + 1, i + lookahead)):
                    obs.append(OrderBlock(
                        type="bullish",
                        top=max(c["open"], c["close"]),   # body top
                        bottom=c["low"],
                        formed_at=i,
                    ))

        elif is_bullish:
            # Potential bearish OB: measure downward impulse
            impulse = (c["close"] - future_close) / c["close"]
            if impulse >= OB_MIN_IMPULSE_PCT:
                if all(df.iloc[j]["close"] <= df.iloc[j]["open"] for j in range(i + 1, i + lookahead)):
                    obs.append(OrderBlock(
                        type="bearish",
                        top=c["high"],
                        bottom=min(c["open"], c["close"]),  # body bottom
                        formed_at=i,
                    ))

    # Mark OBs that have been mitigated (price traded through them)
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
#  Signal generation                                                   #
# ------------------------------------------------------------------ #

def _recency_score(formed_at: int, total_candles: int) -> float:
    """Older zones get lower scores; zones formed in last 20 candles score ~1."""
    age = total_candles - formed_at
    return max(0.0, 1.0 - age / max(total_candles, 1))


def generate_signal(symbol: str, df: pd.DataFrame) -> Optional[TradeSignal]:
    """
    Returns a TradeSignal if the current price is entering a valid bullish zone,
    or None if no qualifying setup exists.

    Only bullish zones are considered (spot long-only strategy).
    Confluence between an FVG and an OB at the same price level boosts strength.
    """
    if len(df) < max(ATR_PERIOD + 5, OB_LOOKBACK + 5):
        return None

    current_price = float(df.iloc[-1]["close"])
    current_atr = float(_atr(df).iloc[-1])
    n = len(df)

    # Limit scan to recent candles to avoid acting on ancient zones
    scan_window = min(n, OB_LOOKBACK * 2)

    fvgs = detect_fvgs(df.iloc[-scan_window:].reset_index(drop=True))
    obs = detect_order_blocks(df.iloc[-scan_window:].reset_index(drop=True))

    active_bull_fvgs = [f for f in fvgs if f.type == "bullish" and not f.filled]
    active_bull_obs = [o for o in obs if o.type == "bullish" and not o.mitigated]

    best: Optional[TradeSignal] = None
    best_strength = 0.0

    def _make_signal(bottom: float, zone_type: str, strength: float) -> TradeSignal:
        sl = bottom - current_atr * ATR_SL_MULT
        risk = current_price - sl
        tp = current_price + risk * TAKE_PROFIT_RR
        return TradeSignal(
            symbol=symbol,
            side="BUY",
            entry_price=current_price,
            stop_loss=max(sl, 0.0),
            take_profit=tp,
            zone_type=zone_type,
            strength=strength,
        )

    # --- Check FVGs ---
    for fvg in active_bull_fvgs:
        if fvg.bottom <= current_price <= fvg.top:
            strength = _recency_score(fvg.formed_at, scan_window)
            if strength > best_strength:
                best_strength = strength
                best = _make_signal(fvg.bottom, "FVG", strength)

    # --- Check OBs (higher intrinsic priority) ---
    for ob in active_bull_obs:
        if ob.bottom <= current_price <= ob.top:
            strength = _recency_score(ob.formed_at, scan_window) + 0.15  # OB bonus
            strength = min(strength, 1.0)

            # Confluence: is there also a bullish FVG overlapping this OB?
            overlapping_fvg = any(
                not f.filled
                and f.type == "bullish"
                and not (f.top < ob.bottom or f.bottom > ob.top)
                for f in active_bull_fvgs
            )
            if overlapping_fvg:
                strength = min(strength + 0.20, 1.0)
                zone_label = "FVG+OB"
            else:
                zone_label = "OB"

            if strength > best_strength:
                best_strength = strength
                best = _make_signal(ob.bottom, zone_label, strength)

    return best
