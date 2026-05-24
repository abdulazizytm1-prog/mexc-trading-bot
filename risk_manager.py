"""
Risk management layer.

Responsibilities:
  - Per-trade position sizing based on fixed fractional risk
  - Daily loss cap: halt new entries once intraday drawdown hits the limit
  - Maximum concurrent positions cap
  - Exit signal detection (stop loss / take profit) from live price
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

from config import (
    DAILY_LOSS_CAP_PCT,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_PCT_OF_BALANCE,
    MAX_RISK_PER_TRADE_PCT,
)


@dataclass
class Position:
    symbol: str
    side: str           # "BUY" (spot long only)
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    order_id: str
    zone_type: str      # "FVG" | "OB" | "FVG+OB"


class RiskManager:
    def __init__(self):
        self._positions: Dict[str, Position] = {}
        self._daily_pnl: float = 0.0
        self._pnl_date: date = date.today()
        self._session_start_balance: float = 0.0

    # ------------------------------------------------------------------ #
    #  Balance & daily tracking                                            #
    # ------------------------------------------------------------------ #

    def set_session_balance(self, balance: float) -> None:
        """Call once at startup with the opening USDT balance."""
        self._session_start_balance = balance

    def _reset_daily_if_new_day(self) -> None:
        today = date.today()
        if today != self._pnl_date:
            self._daily_pnl = 0.0
            self._pnl_date = today

    def record_closed_pnl(self, pnl: float) -> None:
        self._reset_daily_if_new_day()
        self._daily_pnl += pnl

    @property
    def daily_pnl(self) -> float:
        self._reset_daily_if_new_day()
        return self._daily_pnl

    def daily_loss_cap_reached(self) -> bool:
        self._reset_daily_if_new_day()
        if self._session_start_balance <= 0:
            return False
        loss_fraction = -self._daily_pnl / self._session_start_balance
        return loss_fraction >= DAILY_LOSS_CAP_PCT

    def daily_loss_pct(self) -> float:
        """Returns current intraday loss as a positive percentage (e.g. 2.3 means −2.3%)."""
        if self._session_start_balance <= 0:
            return 0.0
        return max(0.0, -self._daily_pnl / self._session_start_balance * 100)

    # ------------------------------------------------------------------ #
    #  Position gate                                                       #
    # ------------------------------------------------------------------ #

    def can_open_position(self, symbol: str) -> bool:
        """Returns True only when all risk gates allow a new entry."""
        if symbol in self._positions:
            return False  # already in this pair
        if len(self._positions) >= MAX_OPEN_POSITIONS:
            return False
        if self.daily_loss_cap_reached():
            return False
        return True

    # ------------------------------------------------------------------ #
    #  Position sizing                                                     #
    # ------------------------------------------------------------------ #

    def calculate_quantity(
        self,
        balance: float,
        entry_price: float,
        stop_loss: float,
        qty_step: float = 0.0,
        min_qty: float = 0.0,
        min_notional: float = 5.0,
    ) -> Optional[float]:
        """
        Returns base-asset quantity to buy, or None if no valid size exists.

        Sizing logic:
          risk_amount = balance * MAX_RISK_PER_TRADE_PCT
          qty         = risk_amount / |entry - stop_loss|
          Then floored to qty_step and capped at MAX_POSITION_PCT_OF_BALANCE.
        """
        price_risk = abs(entry_price - stop_loss)
        if price_risk <= 0 or entry_price <= 0:
            return None

        risk_usdt = balance * MAX_RISK_PER_TRADE_PCT
        raw_qty = risk_usdt / price_risk

        # Floor to exchange lot step
        if qty_step > 0:
            raw_qty = math.floor(raw_qty / qty_step) * qty_step

        # Hard cap: don't commit more than MAX_POSITION_PCT_OF_BALANCE
        max_spend = balance * MAX_POSITION_PCT_OF_BALANCE
        if raw_qty * entry_price > max_spend:
            raw_qty = max_spend / entry_price
            if qty_step > 0:
                raw_qty = math.floor(raw_qty / qty_step) * qty_step

        # Reject if below exchange minimums
        if min_qty > 0 and raw_qty < min_qty:
            return None
        if raw_qty * entry_price < min_notional:
            return None

        return round(raw_qty, 8)

    # ------------------------------------------------------------------ #
    #  Position CRUD                                                       #
    # ------------------------------------------------------------------ #

    def add_position(self, position: Position) -> None:
        self._positions[position.symbol] = position

    def remove_position(self, symbol: str) -> Optional[Position]:
        return self._positions.pop(symbol, None)

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def all_positions(self) -> Dict[str, Position]:
        return dict(self._positions)

    def open_position_count(self) -> int:
        return len(self._positions)

    # ------------------------------------------------------------------ #
    #  Exit conditions                                                     #
    # ------------------------------------------------------------------ #

    def check_exit(self, symbol: str, current_price: float) -> Optional[str]:
        """
        Returns "STOP_LOSS", "TAKE_PROFIT", or None.
        Spot long positions only — price below SL or above TP.
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        if pos.side == "BUY":
            if current_price <= pos.stop_loss:
                return "STOP_LOSS"
            if current_price >= pos.take_profit:
                return "TAKE_PROFIT"
        return None
