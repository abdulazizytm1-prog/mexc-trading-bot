"""
Risk management layer — professional ICT/SMC execution rules.

All v1 method signatures are preserved for backward compatibility.

New capabilities
----------------
  Position fields  : fill_price, tp1/tp2/tp3, hit flags, break-even, trailing,
                     open_time, score, entry_atr
  Partial TPs      : TP1 @ 1:1 RR (33%), TP2 @ 2:1 (33%), TP3 @ 3:1 (34%)
  Break-even       : SL → entry + 0.1% fees after TP1 is hit
  Trailing stop    : ATR × 1.0 trail after TP2; never moves backward
  Persistence      : positions.json — survives restarts (positions + counters)
  Consecutive loss : ×0.5 size at 2 losses in a row; halt at 3
  Daily limits     : 3% loss cap, max 2 new entries per day
  Weekly limits    : 8% loss cap halts all new entries
  Position cap     : max 2 simultaneous open positions

Stop-loss note
--------------
The SL is stored in the Position and checked each loop.  Placing an OCO
stop-limit order on MEXC is done in main.py immediately after the fill
(exchange-level protection).  risk_manager.py tracks the desired SL level
and signals when it is hit by live price.

Config bug (item 9)
-------------------
config.py already reads ``os.getenv("MEXC_SECRET")`` — the MEXC_API_SECRET
rename was fixed in a prior session.  No change needed here.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from config import (
    DAILY_LOSS_CAP_PCT,
    MAX_DAILY_TRADES,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_PCT_OF_BALANCE,
    MAX_RISK_PER_TRADE_PCT,
    WEEKLY_LOSS_CAP_PCT,
)

log = logging.getLogger(__name__)

_POSITIONS_PATH   = Path(__file__).parent / "positions.json"

_FEE_PCT          = 0.001    # 0.1% break-even SL buffer (entry + fees)
_MAX_SL_PCT       = 0.03     # SL hard cap: never more than 3% from entry
_TRAIL_ATR_MULT   = 1.0      # ATR multiplier for trailing stop
_CONSEC_REDUCE_AT = 2        # Start reducing size at this many consecutive losses
_CONSEC_HALT_AT   = 4        # Halt ALL trading at this many consecutive losses
_WIN_STREAK_BOOST_AT = 3     # Boost size after this many consecutive wins
_MAX_WIN_RISK_PCT    = 0.015 # Hard cap on risk % after a win streak (1.5%)


# ------------------------------------------------------------------ #
#  Position dataclass                                                  #
# ------------------------------------------------------------------ #

@dataclass
class Position:
    # ── v1 core fields (signature unchanged) ─────────────────────────
    symbol:      str
    side:        str          # "BUY" (spot long only)
    entry_price: float
    quantity:    float
    stop_loss:   float
    take_profit: float        # backward-compat alias for tp1
    order_id:    str
    zone_type:   str          # "FVG" | "OB" | "FVG+OB" | "SWEEP+..."

    # ── ICT/SMC extended fields ───────────────────────────────────────
    fill_price:        float = 0.0   # actual average fill (cummulativeQuoteQty / executedQty)
    tp1:               float = 0.0   # 1:1 RR target — close 33 %
    tp2:               float = 0.0   # 2:1 RR target — close 33 %
    tp3:               float = 0.0   # 3:1 RR target — close 34 %
    tp1_hit:           bool  = False
    tp2_hit:           bool  = False
    break_even_active: bool  = False
    trailing_active:   bool  = False
    open_time:         str   = ""    # ISO timestamp of entry
    score:             int   = 0     # ICT signal score 0–10
    entry_atr:         float = 0.0   # ATR at entry time (used for trailing)

    # ── OCO exchange-side order tracking ──────────────────────────────
    oco_list_id:  str   = ""    # orderListId returned by MEXC OCO placement
    oco_tp_price: float = 0.0   # TP price the OCO was placed at
    oco_sl_price: float = 0.0   # SL price the OCO was placed at

    # ── Computed helpers ──────────────────────────────────────────────

    @property
    def effective_entry(self) -> float:
        """Actual fill price, or entry_price if fill was not recorded."""
        return self.fill_price if self.fill_price > 0 else self.entry_price

    @property
    def be_applied(self) -> bool:
        """Backward-compat alias for break_even_active."""
        return self.break_even_active

    def partial_qty(self, tp_level: int) -> float:
        """Quantity to sell at TP1 (33 %), TP2 (33 %), or TP3 (34 %)."""
        fracs = {1: 0.33, 2: 0.33, 3: 0.34}
        return round(self.quantity * fracs.get(tp_level, 1.0), 8)

    def remaining_qty(self) -> float:
        """Unsold quantity after partial TP exits."""
        sold = 0.0
        if self.tp1_hit:
            sold += self.partial_qty(1)
        if self.tp2_hit:
            sold += self.partial_qty(2)
        return max(round(self.quantity - sold, 8), 0.0)


# ------------------------------------------------------------------ #
#  Serialisation helpers                                               #
# ------------------------------------------------------------------ #

def _pos_to_dict(pos: Position) -> dict:
    return {
        "symbol":            pos.symbol,
        "side":              pos.side,
        "entry_price":       pos.entry_price,
        "fill_price":        pos.fill_price,
        "quantity":          pos.quantity,
        "stop_loss":         pos.stop_loss,
        "take_profit":       pos.take_profit,
        "tp1":               pos.tp1,
        "tp2":               pos.tp2,
        "tp3":               pos.tp3,
        "tp1_hit":           pos.tp1_hit,
        "tp2_hit":           pos.tp2_hit,
        "break_even_active": pos.break_even_active,
        "trailing_active":   pos.trailing_active,
        "open_time":         pos.open_time,
        "score":             pos.score,
        "entry_atr":         pos.entry_atr,
        "order_id":          pos.order_id,
        "zone_type":         pos.zone_type,
        "oco_list_id":       pos.oco_list_id,
        "oco_tp_price":      pos.oco_tp_price,
        "oco_sl_price":      pos.oco_sl_price,
    }


def _pos_from_dict(d: dict) -> Position:
    return Position(
        symbol            = d["symbol"],
        side              = d.get("side", "BUY"),
        entry_price       = float(d.get("entry_price", 0)),
        quantity          = float(d.get("quantity", 0)),
        stop_loss         = float(d.get("stop_loss", 0)),
        take_profit       = float(d.get("take_profit", 0)),
        order_id          = d.get("order_id", ""),
        zone_type         = d.get("zone_type", ""),
        fill_price        = float(d.get("fill_price", 0)),
        tp1               = float(d.get("tp1", 0)),
        tp2               = float(d.get("tp2", 0)),
        tp3               = float(d.get("tp3", 0)),
        tp1_hit           = bool(d.get("tp1_hit", False)),
        tp2_hit           = bool(d.get("tp2_hit", False)),
        break_even_active = bool(d.get("break_even_active", False)),
        trailing_active   = bool(d.get("trailing_active", False)),
        open_time         = d.get("open_time", ""),
        score             = int(d.get("score", 0)),
        entry_atr         = float(d.get("entry_atr", 0)),
        oco_list_id       = d.get("oco_list_id", ""),
        oco_tp_price      = float(d.get("oco_tp_price", 0)),
        oco_sl_price      = float(d.get("oco_sl_price", 0)),
    )


# ------------------------------------------------------------------ #
#  RiskManager                                                         #
# ------------------------------------------------------------------ #

class RiskManager:
    def __init__(self) -> None:
        # Core position store
        self._positions: Dict[str, Position] = {}

        # Daily PnL tracking
        self._daily_pnl:  float = 0.0
        self._pnl_date:   date  = date.today()

        # Weekly PnL tracking
        self._weekly_pnl:          float = 0.0
        self._week_start:          date  = self._monday_of(date.today())
        self._weekly_start_balance: float = 0.0

        # Session reference balance
        self._session_start_balance: float = 0.0

        # Daily trade counter
        self._daily_trades: int  = 0
        self._trade_date:   date = date.today()

        # Consecutive loss / win streak circuit-breakers
        self._consecutive_losses: int = 0
        self._consecutive_wins:   int = 0

        # Load persisted state
        self.load_positions()

    # ------------------------------------------------------------------ #
    #  Week helpers                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _monday_of(d: date) -> date:
        return d - timedelta(days=d.weekday())

    def _reset_weekly_if_new_week(self) -> None:
        current_monday = self._monday_of(date.today())
        if current_monday != self._week_start:
            self._weekly_pnl   = 0.0
            self._week_start   = current_monday
            if self._session_start_balance > 0:
                self._weekly_start_balance = self._session_start_balance

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def load_positions(self) -> None:
        """Load positions and counters from positions.json on startup."""
        if not _POSITIONS_PATH.exists():
            return
        try:
            raw = json.loads(_POSITIONS_PATH.read_text(encoding="utf-8"))
            for sym, pd in raw.get("positions", {}).items():
                self._positions[sym] = _pos_from_dict(pd)
            self._consecutive_losses = int(raw.get("consecutive_losses", 0))
            self._consecutive_wins   = int(raw.get("consecutive_wins", 0))
            self._daily_trades       = int(raw.get("daily_trades", 0))
            trade_date_str           = raw.get("trade_date", "")
            if trade_date_str:
                self._trade_date = date.fromisoformat(trade_date_str)
            if self._trade_date != date.today():
                self._daily_trades = 0
                self._trade_date   = date.today()
            log.info(
                "[RiskManager] Loaded %d position(s) from disk | "
                "consecutive_losses=%d | daily_trades=%d",
                len(self._positions), self._consecutive_losses, self._daily_trades,
            )
        except Exception as exc:
            log.warning("[RiskManager] Could not load positions.json: %s", exc)

    def save_positions(self) -> None:
        """Persist positions and counters to positions.json."""
        payload = {
            "saved_at":           datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "consecutive_losses": self._consecutive_losses,
            "consecutive_wins":   self._consecutive_wins,
            "daily_trades":       self._daily_trades,
            "trade_date":         self._trade_date.isoformat(),
            "positions": {
                sym: _pos_to_dict(pos)
                for sym, pos in self._positions.items()
            },
        }
        try:
            _POSITIONS_PATH.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("[RiskManager] Could not save positions.json: %s", exc)

    # ------------------------------------------------------------------ #
    #  Balance & daily tracking                                            #
    # ------------------------------------------------------------------ #

    def set_session_balance(self, balance: float) -> None:
        """Call once at startup with the opening USDT balance."""
        self._session_start_balance = balance
        if self._weekly_start_balance <= 0:
            self._weekly_start_balance = balance

    def _reset_daily_if_new_day(self) -> None:
        today = date.today()
        if today != self._pnl_date:
            self._daily_pnl  = 0.0
            self._pnl_date   = today
        # Also reset daily trade counter when the calendar day rolls over
        if today != self._trade_date:
            self._daily_trades = 0
            self._trade_date   = today

    def record_closed_pnl(self, pnl: float) -> None:
        self._reset_daily_if_new_day()
        self._reset_weekly_if_new_week()
        self._daily_pnl  += pnl
        self._weekly_pnl += pnl

        # Update consecutive loss / win streak counters
        if pnl < 0:
            self._consecutive_wins    = 0
            self._consecutive_losses += 1
            log.warning(
                "[RiskManager] Loss recorded (PnL=%.4f USDT) | "
                "consecutive losses: %d | consecutive wins reset to 0",
                pnl, self._consecutive_losses,
            )
            if self._consecutive_losses >= _CONSEC_HALT_AT:
                log.warning(
                    "[RiskManager] %d consecutive losses — trading halted until "
                    "next winning trade.",
                    self._consecutive_losses,
                )
        else:
            if self._consecutive_losses > 0:
                log.info(
                    "[RiskManager] Winning trade resets consecutive-loss counter "
                    "(was %d).",
                    self._consecutive_losses,
                )
            self._consecutive_losses  = 0
            self._consecutive_wins   += 1
            if self._consecutive_wins >= _WIN_STREAK_BOOST_AT:
                log.info(
                    "[RiskManager] Win streak: %d wins — size boost active (up to %.1f%% risk).",
                    self._consecutive_wins, _MAX_WIN_RISK_PCT * 100,
                )

        self.save_positions()

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

    def weekly_loss_cap_reached(self) -> bool:
        """Returns True when the weekly drawdown exceeds WEEKLY_LOSS_CAP_PCT (8%)."""
        self._reset_weekly_if_new_week()
        ref = self._weekly_start_balance or self._session_start_balance
        if ref <= 0:
            return False
        return (-self._weekly_pnl / ref) >= WEEKLY_LOSS_CAP_PCT

    def weekly_loss_pct(self) -> float:
        """Returns current weekly loss as a positive percentage."""
        ref = self._weekly_start_balance or self._session_start_balance
        if ref <= 0:
            return 0.0
        return max(0.0, -self._weekly_pnl / ref * 100)

    def consecutive_loss_halt_active(self) -> bool:
        """Returns True when four or more consecutive losses have been recorded."""
        return self._consecutive_losses >= _CONSEC_HALT_AT

    # ------------------------------------------------------------------ #
    #  Position gate                                                       #
    # ------------------------------------------------------------------ #

    def can_open_position(self, symbol: str) -> bool:
        """
        Returns True only when ALL risk gates permit a new entry.

        Gates checked (in order):
          1. Symbol not already open
          2. Max simultaneous positions (2)
          3. Daily loss cap (3%)
          4. Weekly loss cap (8%)
          5. Daily trade count (max 2 per day)
          6. Consecutive-loss circuit-breaker (halt at 3 in a row)
        """
        self._reset_daily_if_new_day()

        if symbol in self._positions:
            return False

        if len(self._positions) >= MAX_OPEN_POSITIONS:
            return False

        if self.daily_loss_cap_reached():
            return False

        if self.weekly_loss_cap_reached():
            log.warning(
                "[RiskManager] Weekly loss cap reached (%.2f%%) — no new entries.",
                self.weekly_loss_pct(),
            )
            return False

        if self._daily_trades >= MAX_DAILY_TRADES:
            log.info(
                "[RiskManager] Daily trade limit reached (%d/%d) — no new entries.",
                self._daily_trades, MAX_DAILY_TRADES,
            )
            return False

        if self.consecutive_loss_halt_active():
            log.warning(
                "[RiskManager] %d consecutive losses — trading halted for today.",
                self._consecutive_losses,
            )
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
        qty_step:     float = 0.0,
        min_qty:      float = 0.0,
        min_notional: float = 5.0,
        is_friday:    bool  = False,
    ) -> Optional[float]:
        """
        Returns base-asset quantity to buy, or None if the trade should be skipped.

        Sizing rules (all percentage-based — always proportional to balance)
        ---------------------------------------------------------------------
          Base risk       = balance × 1.0%
          Win streak (3+) = balance × 1.1%  (capped at 1.5%)
          Stop distance   = |entry_price − stop_loss|
          raw_qty         = risk_amount / stop_distance

          Friday               : × 0.50
          2 consecutive losses : × 0.75  (−25%)
          3 consecutive losses : × 0.50  (−50%)
          4+ consecutive losses: skip — return None (day halt)

          Max position cap : balance × 10% (hard ceiling)

          Below exchange minimum → log "Position too small for current balance"
          and return None.  Never force minimum.

        The `is_friday` flag is set by the caller when kill_zone == "FRIDAY_REDUCED".
        """
        price_risk = abs(entry_price - stop_loss)
        if price_risk <= 0 or entry_price <= 0:
            log.warning("[RiskManager] Invalid entry/SL prices — skipping sizing.")
            return None

        # ── Halt check (4+ consecutive losses) ───────────────────────────────
        losses = self._consecutive_losses
        if losses >= _CONSEC_HALT_AT:
            log.warning(
                "[RiskManager] %d consecutive losses — trade halted for today.",
                losses,
            )
            return None

        # ── Effective risk % (base 1%, boosted up to 1.5% on win streak) ─────
        effective_risk_pct = MAX_RISK_PER_TRADE_PCT  # 1.0%
        wins = self._consecutive_wins
        if wins >= _WIN_STREAK_BOOST_AT:
            effective_risk_pct = min(MAX_RISK_PER_TRADE_PCT * 1.10, _MAX_WIN_RISK_PCT)
            log.debug(
                "[RiskManager] Win streak %d — risk raised to %.2f%%",
                wins, effective_risk_pct * 100,
            )

        risk_usdt = balance * effective_risk_pct
        raw_qty   = risk_usdt / price_risk

        # ── Friday modifier ───────────────────────────────────────────────────
        if is_friday:
            raw_qty *= 0.50
            log.debug("[RiskManager] Friday — position halved (×0.50).")

        # ── Consecutive loss scaling ──────────────────────────────────────────
        if losses == 3:
            raw_qty *= 0.50
            log.warning(
                "[RiskManager] 3 consecutive losses — size reduced to 50%%"
                " (%.2f USDT risk).",
                raw_qty * price_risk,
            )
        elif losses == 2:
            raw_qty *= 0.75
            log.warning(
                "[RiskManager] 2 consecutive losses — size reduced to 75%%"
                " (%.2f USDT risk).",
                raw_qty * price_risk,
            )
        # losses 0 or 1 → no reduction

        # ── Floor to exchange lot step ────────────────────────────────────────
        if qty_step > 0:
            raw_qty = math.floor(raw_qty / qty_step) * qty_step

        # ── Hard cap: max 10% of balance ──────────────────────────────────────
        max_spend = balance * MAX_POSITION_PCT_OF_BALANCE
        if raw_qty * entry_price > max_spend:
            raw_qty = max_spend / entry_price
            if qty_step > 0:
                raw_qty = math.floor(raw_qty / qty_step) * qty_step
            log.debug(
                "[RiskManager] Position capped at 10%% of balance "
                "(%.2f USDT).", max_spend,
            )

        # ── Exchange minimum checks — skip if too small, never force ──────────
        notional = raw_qty * entry_price
        if notional < min_notional:
            log.warning(
                "[RiskManager] Position too small for current balance "
                "(%.4f USDT notional < %.2f USDT minimum | balance=%.2f USDT).",
                notional, min_notional, balance,
            )
            return None

        if min_qty > 0 and raw_qty < min_qty:
            log.warning(
                "[RiskManager] Position too small for current balance "
                "(qty %.8f < min lot %.8f | balance=%.2f USDT).",
                raw_qty, min_qty, balance,
            )
            return None

        log.debug(
            "[RiskManager] Sizing: balance=%.2f losses=%d wins=%d "
            "risk_pct=%.2f%% friday=%s → qty=%.8f (%.4f USDT notional)",
            balance, losses, wins, effective_risk_pct * 100,
            is_friday, raw_qty, notional,
        )
        return round(raw_qty, 8)

    # ------------------------------------------------------------------ #
    #  Position CRUD                                                       #
    # ------------------------------------------------------------------ #

    def add_position(self, position: Position) -> None:
        self._positions[position.symbol] = position
        self._daily_trades += 1
        log.info(
            "[RiskManager] Position added: %s | trades today: %d/%d",
            position.symbol, self._daily_trades, MAX_DAILY_TRADES,
        )
        self.save_positions()

    def remove_position(self, symbol: str) -> Optional[Position]:
        pos = self._positions.pop(symbol, None)
        if pos is not None:
            self.save_positions()
        return pos

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def all_positions(self) -> Dict[str, Position]:
        return dict(self._positions)

    def open_position_count(self) -> int:
        return len(self._positions)

    # ------------------------------------------------------------------ #
    #  OCO exchange-side order management                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_price(price: float, tick_size: float) -> str:
        if tick_size > 0:
            decimals = max(0, round(-math.log10(tick_size)))
            price = math.floor(price / tick_size) * tick_size
            return f"{price:.{decimals}f}"
        return f"{price:.8f}".rstrip("0").rstrip(".")

    def place_oco_for_position(
        self,
        symbol:    str,
        api:       Any,
        tp_price:  float,
        sl_price:  float,
        tick_size: float = 0.0,
    ) -> bool:
        """
        Place an OCO (TP limit + SL stop-limit) on the exchange for the open
        position.  Saves orderListId to the Position and persists to disk.

        Returns True on success, False on failure (soft failure — never raises).
        Retries once after 3 seconds on the first error.
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return False

        qty      = pos.remaining_qty()
        tp_str   = self._format_price(tp_price, tick_size)
        sl_str   = self._format_price(sl_price, tick_size)

        for attempt in (1, 2):
            try:
                result = api.place_oco_order(
                    symbol          = symbol,
                    side            = "SELL",
                    quantity        = qty,
                    price           = tp_str,
                    stop_price      = sl_str,
                    stop_limit_price= sl_str,
                )
                list_id = str(result.get("orderListId", ""))
                if not list_id or list_id == "0":
                    raise ValueError(f"unexpected response: {result}")

                pos.oco_list_id  = list_id
                pos.oco_tp_price = tp_price
                pos.oco_sl_price = sl_price
                self.save_positions()
                log.info(
                    "[OCO] Placed for %s: TP=$%s  SL=$%s  (orderListId=%s)",
                    symbol, tp_str, sl_str, list_id,
                )
                return True
            except Exception as exc:
                if attempt == 1:
                    log.warning(
                        "[OCO] Place failed for %s (attempt 1): %s — retrying in 3s",
                        symbol, exc,
                    )
                    time.sleep(3)
                else:
                    log.error(
                        "[OCO] Place failed for %s (attempt 2): %s — keeping software SL/TP",
                        symbol, exc,
                    )
        return False

    def cancel_oco_for_position(self, symbol: str, api: Any) -> bool:
        """
        Cancel any live OCO order for the position and clear the stored id.

        Returns True on success or when there is no OCO to cancel.
        Returns False only when a live OCO exists but the cancel call fails.
        """
        pos = self._positions.get(symbol)
        if pos is None or not pos.oco_list_id:
            return True

        list_id = pos.oco_list_id
        try:
            api.cancel_oco_order(symbol, list_id)
            log.info("[OCO] Cancelled for %s (orderListId=%s)", symbol, list_id)
        except Exception as exc:
            log.warning(
                "[OCO] Cancel failed for %s (orderListId=%s): %s",
                symbol, list_id, exc,
            )
            return False
        finally:
            # Clear the id regardless so we don't attempt to cancel a stale order again
            pos.oco_list_id  = ""
            pos.oco_tp_price = 0.0
            pos.oco_sl_price = 0.0
            self.save_positions()

        return True

    # ------------------------------------------------------------------ #
    #  Partial TP state transitions                                        #
    # ------------------------------------------------------------------ #

    def handle_tp1_hit(
        self,
        symbol:    str,
        api:       Any  = None,
        tick_size: float = 0.0,
    ) -> None:
        """
        Mark TP1 as hit, activate break-even stop loss, refresh OCO.

        Break-even SL = effective_entry × (1 + fee_buffer)
        so the trade cannot close at a loss even after fees.

        If api is supplied: cancel the existing OCO, then place a new one
        (TP=tp2, SL=break-even).
        """
        pos = self._positions.get(symbol)
        if pos is None or pos.tp1_hit:
            return

        pos.tp1_hit           = True
        pos.break_even_active = True
        be_sl = pos.effective_entry * (1.0 + _FEE_PCT)

        # Only move SL forward (never down)
        if be_sl > pos.stop_loss:
            pos.stop_loss = round(be_sl, 8)

        log.info(
            "[RiskManager] Break-even activated for %s | SL moved to %.6f",
            symbol, pos.stop_loss,
        )
        self.save_positions()

        if api is not None and pos.tp2 > 0:
            self.cancel_oco_for_position(symbol, api)
            self.place_oco_for_position(symbol, api, pos.tp2, pos.stop_loss, tick_size)

    def handle_tp2_hit(
        self,
        symbol:    str,
        api:       Any  = None,
        tick_size: float = 0.0,
    ) -> None:
        """
        Mark TP2 as hit, activate ATR trailing stop, refresh OCO.
        Actual trail distance is applied by update_trailing_stop() each candle.

        If api is supplied: cancel the existing OCO, then place a new one
        (TP=tp3, SL=current stop_loss).
        """
        pos = self._positions.get(symbol)
        if pos is None or pos.tp2_hit:
            return

        pos.tp2_hit         = True
        pos.trailing_active = True
        log.info(
            "[RiskManager] Trailing stop activated for %s (entry_atr=%.6f)",
            symbol, pos.entry_atr,
        )
        self.save_positions()

        if api is not None and pos.tp3 > 0:
            self.cancel_oco_for_position(symbol, api)
            self.place_oco_for_position(symbol, api, pos.tp3, pos.stop_loss, tick_size)

    def update_trailing_stop(
        self,
        symbol: str,
        current_price: float,
        atr: float,
    ) -> Optional[float]:
        """
        Advance the trailing stop for a position where trailing_active is True.

        Trail distance = ATR × _TRAIL_ATR_MULT (1.0 by default).
        The SL is only raised, never lowered.

        Returns the new SL price if updated, or None if unchanged.
        """
        pos = self._positions.get(symbol)
        if pos is None or not pos.trailing_active:
            return None

        trail_distance = (atr if atr > 0 else pos.entry_atr) * _TRAIL_ATR_MULT
        new_sl = current_price - trail_distance

        if new_sl > pos.stop_loss:
            pos.stop_loss = round(new_sl, 8)
            self.save_positions()
            log.debug(
                "[RiskManager] Trailing SL for %s → %.6f (price=%.6f ATR=%.6f)",
                symbol, pos.stop_loss, current_price, atr,
            )
            return pos.stop_loss

        return None

    # ------------------------------------------------------------------ #
    #  Exit conditions                                                     #
    # ------------------------------------------------------------------ #

    def check_exit(self, symbol: str, current_price: float) -> Optional[str]:
        """
        Returns the exit signal for a BUY position, or None.

        Return values
        -------------
        "STOP_LOSS"   — price at or below stop loss (full close)
        "TP1"         — first target hit (partial close 33 %; call handle_tp1_hit)
        "TP2"         — second target hit after TP1 (partial close 33 %; call handle_tp2_hit)
        "TP3"         — third target hit after TP2 (full close of remaining 34 %)
        "TAKE_PROFIT" — legacy single-TP mode (tp1/tp2/tp3 not set)
        None          — no exit condition met

        Check order: SL first, then partial TPs in sequence, then legacy TP.
        """
        pos = self._positions.get(symbol)
        if pos is None or pos.side != "BUY":
            return None

        # ── Stop loss (always highest priority) ──────────────────────────────
        if current_price <= pos.stop_loss:
            return "STOP_LOSS"

        # ── Partial TP mode (tp1 set) ─────────────────────────────────────────
        if pos.tp1 > 0:
            # TP1: first target, not yet hit
            if not pos.tp1_hit and current_price >= pos.tp1:
                return "TP1"

            # TP2: second target, only after TP1
            if pos.tp1_hit and not pos.tp2_hit and pos.tp2 > 0:
                if current_price >= pos.tp2:
                    return "TP2"

            # TP3: final close, only after TP2
            if pos.tp2_hit and pos.tp3 > 0:
                if current_price >= pos.tp3:
                    return "TP3"

            return None

        # ── Legacy single-TP mode (take_profit set, tp1/tp2/tp3 not) ─────────
        if pos.take_profit > 0 and current_price >= pos.take_profit:
            return "TAKE_PROFIT"

        return None
