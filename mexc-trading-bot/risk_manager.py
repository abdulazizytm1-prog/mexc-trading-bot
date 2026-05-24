import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from config import DAILY_LOSS_CAP_PCT, MAX_POSITION_PCT, RISK_PER_TRADE_PCT

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    entry: float
    quantity: float
    stop_loss: float
    take_profit: float


class RiskManager:
    """
    Enforces per-trade position sizing and a rolling daily loss cap.

    Position size is the smaller of:
      • (account_balance × RISK_PER_TRADE_PCT) / price_risk_per_unit
      • (account_balance × MAX_POSITION_PCT)  / entry_price

    Trading halts for the day once realised losses exceed
    account_balance × DAILY_LOSS_CAP_PCT.
    """

    def __init__(self):
        self._positions: dict[str, Position] = {}
        self._daily_loss: float = 0.0
        self._trade_date: date = date.today()

    # ------------------------------------------------------------------
    # Daily loss tracking
    # ------------------------------------------------------------------

    def _refresh_day(self) -> None:
        today = date.today()
        if today != self._trade_date:
            logger.info(
                "New trading day — resetting daily loss (was %.4f USDT).",
                self._daily_loss,
            )
            self._daily_loss = 0.0
            self._trade_date = today

    def daily_loss_exceeded(self, account_balance: float) -> bool:
        self._refresh_day()
        cap = account_balance * DAILY_LOSS_CAP_PCT
        if self._daily_loss >= cap:
            logger.warning(
                "Daily loss cap reached: %.4f / %.4f USDT.", self._daily_loss, cap
            )
            return True
        return False

    @property
    def daily_loss(self) -> float:
        return self._daily_loss

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    @property
    def open_symbols(self) -> list[str]:
        return list(self._positions.keys())

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def can_open(self, symbol: str, account_balance: float) -> bool:
        if self.has_position(symbol):
            logger.debug("%s: position already open, skipping.", symbol)
            return False
        if self.daily_loss_exceeded(account_balance):
            return False
        return True

    def calculate_quantity(
        self,
        account_balance: float,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """Returns the number of base-asset units to buy, floored to 0."""
        price_risk = entry_price - stop_loss
        if price_risk <= 0:
            logger.warning("Stop-loss >= entry price; skipping size calculation.")
            return 0.0

        risk_qty = (account_balance * RISK_PER_TRADE_PCT) / price_risk
        max_qty = (account_balance * MAX_POSITION_PCT) / entry_price
        return min(risk_qty, max_qty)

    def open_position(self, pos: Position) -> None:
        self._positions[pos.symbol] = pos
        logger.info(
            "Opened %s | entry=%.6g  qty=%.6f  SL=%.6g  TP=%.6g",
            pos.symbol, pos.entry, pos.quantity, pos.stop_loss, pos.take_profit,
        )

    def close_position(self, symbol: str, exit_price: float) -> float:
        """Closes the position, records PnL, and returns realised PnL in USDT."""
        pos = self._positions.pop(symbol, None)
        if pos is None:
            return 0.0

        pnl = (exit_price - pos.entry) * pos.quantity
        if pnl < 0:
            self._daily_loss += abs(pnl)

        logger.info(
            "Closed %s | exit=%.6g  pnl=%.4f USDT  daily_loss=%.4f USDT",
            symbol, exit_price, pnl, self._daily_loss,
        )
        return pnl

    # ------------------------------------------------------------------
    # Exit condition checks
    # ------------------------------------------------------------------

    def check_exit(self, symbol: str, current_price: float) -> Optional[str]:
        """
        Returns 'stop_loss', 'take_profit', or None.
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        if current_price <= pos.stop_loss:
            return "stop_loss"
        if current_price >= pos.take_profit:
            return "take_profit"
        return None
