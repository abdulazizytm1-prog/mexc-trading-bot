from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from config import FVG_MIN_SIZE_PCT, OB_LOOKBACK, SL_BUFFER, TP_RR_RATIO


@dataclass
class FVG:
    top: float
    bottom: float
    direction: str      # 'bullish' | 'bearish'
    candle_index: int


@dataclass
class OrderBlock:
    top: float
    bottom: float
    direction: str      # 'bullish' | 'bearish'
    candle_index: int


@dataclass
class Signal:
    symbol: str
    entry: float
    stop_loss: float
    take_profit: float
    reason: str


class SMCStrategy:
    """
    Smart Money Concepts strategy for spot-only trading.

    Detects Fair Value Gaps and Order Blocks, then generates long-only
    signals when price retests those zones from above (bullish context).
    Short signals are intentionally excluded — spot trading has no short-selling.
    """

    def __init__(self):
        self.fvg_min_size = FVG_MIN_SIZE_PCT
        self.ob_lookback = OB_LOOKBACK
        self.sl_buffer = SL_BUFFER
        self.tp_rr = TP_RR_RATIO

    # ------------------------------------------------------------------
    # Data parsing
    # ------------------------------------------------------------------

    def parse_klines(self, raw: list) -> pd.DataFrame:
        cols = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ]
        df = pd.DataFrame(raw, columns=cols)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"].astype(np.int64), unit="ms")
        return df.set_index("open_time")

    # ------------------------------------------------------------------
    # FVG detection
    # ------------------------------------------------------------------

    def detect_fvgs(self, df: pd.DataFrame) -> list[FVG]:
        """
        A Fair Value Gap forms when a candle's body completely skips price,
        leaving an imbalance between candle[i-2] and candle[i].

        Bullish FVG : candle[i-2].high < candle[i].low  — upward gap
        Bearish FVG : candle[i-2].low  > candle[i].high — downward gap
        """
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        fvgs: list[FVG] = []

        for i in range(2, len(df)):
            h2 = highs[i - 2]
            l2 = lows[i - 2]
            h0 = highs[i]
            l0 = lows[i]

            # Bullish FVG
            if l0 > h2:
                gap_pct = (l0 - h2) / h2
                if gap_pct >= self.fvg_min_size:
                    fvgs.append(FVG(top=l0, bottom=h2, direction="bullish", candle_index=i))

            # Bearish FVG
            elif h0 < l2:
                gap_pct = (l2 - h0) / l2
                if gap_pct >= self.fvg_min_size:
                    fvgs.append(FVG(top=l2, bottom=h0, direction="bearish", candle_index=i))

        return fvgs

    # ------------------------------------------------------------------
    # Order Block detection
    # ------------------------------------------------------------------

    def detect_order_blocks(self, df: pd.DataFrame) -> list[OrderBlock]:
        """
        Bullish OB  : last bearish candle before a 3-candle bullish impulse.
        Bearish OB  : last bullish candle before a 3-candle bearish impulse.

        Only the most recent `ob_lookback` OBs are returned.
        """
        opens = df["open"].to_numpy()
        closes = df["close"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        n = len(df)
        obs: list[OrderBlock] = []

        for i in range(1, n - 3):
            following = slice(i + 1, min(i + 4, n))
            fo = opens[following]
            fc = closes[following]

            # Bullish OB: current candle is bearish, followed by 3 bullish candles
            if closes[i] < opens[i]:
                if np.all(fc > fo):
                    obs.append(OrderBlock(
                        top=highs[i], bottom=lows[i],
                        direction="bullish", candle_index=i,
                    ))

            # Bearish OB: current candle is bullish, followed by 3 bearish candles
            elif closes[i] > opens[i]:
                if np.all(fc < fo):
                    obs.append(OrderBlock(
                        top=highs[i], bottom=lows[i],
                        direction="bearish", candle_index=i,
                    ))

        return obs[-self.ob_lookback:]

    # ------------------------------------------------------------------
    # Signal generation (long-only for spot trading)
    # ------------------------------------------------------------------

    def _make_long_signal(
        self,
        symbol: str,
        current_price: float,
        zone_bottom: float,
        reason: str,
    ) -> Signal:
        stop_loss = zone_bottom * (1.0 - self.sl_buffer)
        risk = current_price - stop_loss
        take_profit = current_price + risk * self.tp_rr
        return Signal(
            symbol=symbol,
            entry=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=reason,
        )

    def generate_signal(
        self, symbol: str, df: pd.DataFrame, current_price: float
    ) -> Optional[Signal]:
        """
        Returns a long Signal if the current price is inside a bullish FVG or
        bullish Order Block, prioritising the most recent zone.
        Returns None if no setup is active.
        """
        fvgs = self.detect_fvgs(df)
        obs = self.detect_order_blocks(df)

        # Check bullish FVGs (newest first)
        for fvg in reversed(fvgs):
            if fvg.direction == "bullish" and fvg.bottom <= current_price <= fvg.top:
                return self._make_long_signal(
                    symbol, current_price, fvg.bottom,
                    f"Bullish FVG [{fvg.bottom:.6g} – {fvg.top:.6g}]",
                )

        # Check bullish Order Blocks (newest first)
        for ob in reversed(obs):
            if ob.direction == "bullish" and ob.bottom <= current_price <= ob.top:
                return self._make_long_signal(
                    symbol, current_price, ob.bottom,
                    f"Bullish OB [{ob.bottom:.6g} – {ob.top:.6g}]",
                )

        return None
