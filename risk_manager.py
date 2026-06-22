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
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (
    CONFIRMATION_EXPIRY_HOURS,
    DAILY_LOSS_CAP_PCT,
    MAX_DAILY_TRADES,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_PCT_OF_BALANCE,
    MAX_RISK_PER_TRADE_PCT,
    SETUP_EXPIRY_HOURS,
    WEEKLY_LOSS_CAP_PCT,
)

log = logging.getLogger(__name__)

_POSITIONS_PATH   = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", str(Path(__file__).parent))) / "positions.json"

_FEE_PCT          = 0.001    # 0.1% break-even SL buffer (entry + fees)
_MAX_SL_PCT       = 0.03     # SL hard cap: never more than 3% from entry
_TRAIL_ATR_MULT   = 1.0      # ATR multiplier for trailing stop
_CONSEC_REDUCE_AT = 2        # Start reducing size at this many consecutive losses
_CONSEC_HALT_AT   = 4        # Halt ALL trading at this many consecutive losses
_WIN_STREAK_BOOST_AT = 3     # Boost size after this many consecutive wins
_MAX_WIN_RISK_PCT    = 0.015 # Hard cap on risk % after a win streak (1.5%)

# ── SetupTracker constants ─────────────────────────────────────────────────── #
_SETUPS_PATH              = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH",
                                str(Path(__file__).parent))) / "setups.json"
_SETUP_EXPIRY_SECS        = SETUP_EXPIRY_HOURS * 3_600
_CONFIRMATION_EXPIRY_SECS = CONFIRMATION_EXPIRY_HOURS * 3_600
_SETUP_HISTORY_KEEP_SECS  = 86_400   # keep EXPIRED/INVALIDATED setups for audit (24H)
_CONFIRMED_COOLDOWN_SECS  = 300      # post-CONFIRMED dedup window prevents same-tick duplicate
                                     # creation and DRY_RUN re-confirmation loops


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

    # ── Exchange-side bracket order tracking ─────────────────────────
    tp_order_id:  str   = ""    # orderId of the resting LIMIT SELL (take-profit)
    sl_order_id:  str   = ""    # orderId of the resting STOP_LOSS_LIMIT SELL (stop-loss)
    oco_tp_price: float = 0.0   # TP price the bracket was placed at
    oco_sl_price: float = 0.0   # SL price the bracket was placed at

    # ── Safety status ─────────────────────────────────────────────────
    # "active"           — normal trading position
    # "quarantine:<why>" — invalid/delisted symbol; excluded from all loops
    # "needs_reconcile"  — balance mismatch or failed exit; manual review needed
    status: str = "active"

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
#  Setup dataclass                                                     #
# ------------------------------------------------------------------ #

@dataclass
class Setup:
    """
    Tracks a detected SMC/ICT setup from identification through confirmation.

    Status flow
    -----------
    IDENTIFIED  → zone not yet touched; expires after _SETUP_EXPIRY_SECS
    ZONE_ENTERED→ price inside OB/FVG; confirmation watch active
    CONFIRMED   → 15M MSS body-close received; entry authorised
    EXPIRED     → timeout elapsed without progressing
    INVALIDATED → structural condition breached before entry
    """
    symbol:                  str
    status:                  str    # IDENTIFIED | ZONE_ENTERED | CONFIRMED | EXPIRED | INVALIDATED
    zone_type:               str    # "FVG" | "OB" | "FVG+OB" | "SWEEP+..."
    zone_high:               float  # = signal.entry_price  (top of OB/FVG zone)
    zone_low:                float  # = signal.stop_loss    (bottom of OB/FVG zone)
    invalidation_price:      float  # price must not CLOSE below this (= zone_low)
    signal_score:            float  # 0.0 – 10.0 from signal generator
    sl_price:                float  # hard SL for the eventual position
    tp1:                     float
    tp2:                     float
    tp3:                     float
    atr_at_detect:           float  # ATR(14) at creation time (used for trail calc later)
    detected_at:             float  # time.time() at creation
    zone_entered_at:         float  # 0.0 until price first touches the zone
    confirmed_at:            float  # 0.0 until 15M MSS confirmation fires
    expires_at:              float  # detected_at + _SETUP_EXPIRY_SECS
    confirmation_expires_at: float  # 0.0 until zone entered; then zone_entered_at + _CONFIRMATION_EXPIRY_SECS
    invalidation_reason:     str    # "" until EXPIRED or INVALIDATED


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
        "tp_order_id":       pos.tp_order_id,
        "sl_order_id":       pos.sl_order_id,
        "oco_tp_price":      pos.oco_tp_price,
        "oco_sl_price":      pos.oco_sl_price,
        "status":            pos.status,
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
        tp_order_id       = d.get("tp_order_id", ""),
        sl_order_id       = d.get("sl_order_id", ""),
        oco_tp_price      = float(d.get("oco_tp_price", 0)),
        oco_sl_price      = float(d.get("oco_sl_price", 0)),
        status            = d.get("status", "active"),
    )


def _setup_to_dict(s: Setup) -> dict:
    return {
        "symbol":                  s.symbol,
        "status":                  s.status,
        "zone_type":               s.zone_type,
        "zone_high":               s.zone_high,
        "zone_low":                s.zone_low,
        "invalidation_price":      s.invalidation_price,
        "signal_score":            s.signal_score,
        "sl_price":                s.sl_price,
        "tp1":                     s.tp1,
        "tp2":                     s.tp2,
        "tp3":                     s.tp3,
        "atr_at_detect":           s.atr_at_detect,
        "detected_at":             s.detected_at,
        "zone_entered_at":         s.zone_entered_at,
        "confirmed_at":            s.confirmed_at,
        "expires_at":              s.expires_at,
        "confirmation_expires_at": s.confirmation_expires_at,
        "invalidation_reason":     s.invalidation_reason,
    }


def _setup_from_dict(d: dict) -> Setup:
    return Setup(
        symbol                  = d["symbol"],
        status                  = d.get("status", "IDENTIFIED"),
        zone_type               = d.get("zone_type", ""),
        zone_high               = float(d.get("zone_high", 0)),
        zone_low                = float(d.get("zone_low", 0)),
        invalidation_price      = float(d.get("invalidation_price", 0)),
        signal_score            = float(d.get("signal_score", 0)),
        sl_price                = float(d.get("sl_price", 0)),
        tp1                     = float(d.get("tp1", 0)),
        tp2                     = float(d.get("tp2", 0)),
        tp3                     = float(d.get("tp3", 0)),
        atr_at_detect           = float(d.get("atr_at_detect", 0)),
        detected_at             = float(d.get("detected_at", 0)),
        zone_entered_at         = float(d.get("zone_entered_at", 0)),
        confirmed_at            = float(d.get("confirmed_at", 0)),
        expires_at              = float(d.get("expires_at", 0)),
        confirmation_expires_at = float(d.get("confirmation_expires_at", 0)),
        invalidation_reason     = d.get("invalidation_reason", ""),
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

        active_count = sum(1 for p in self._positions.values() if p.status == "active")
        if active_count >= MAX_OPEN_POSITIONS:
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

    def active_positions(self) -> Dict[str, Position]:
        """Returns only positions with status == 'active' (excludes quarantined / needs_reconcile)."""
        return {s: p for s, p in self._positions.items() if p.status == "active"}

    def quarantine_position(self, symbol: str, reason: str) -> None:
        """
        Mark a position as quarantined.  It is then excluded from the exit loop,
        global position limits, and new-entry decisions.  The record is kept in
        positions.json for audit; nothing is deleted silently.
        """
        pos = self._positions.get(symbol)
        if pos is None or not pos.status.startswith("active"):
            return
        pos.status = f"quarantine:{reason}"
        self.save_positions()
        log.warning(
            "[RiskManager] %s quarantined (%s) — excluded from exit loop "
            "and position limits. Manual review required.",
            symbol, reason,
        )

    def validate_positions(self, valid_symbols: set) -> None:
        """
        Called at startup with the current MEXC active symbol set.
        Any persisted position whose symbol is no longer on MEXC is quarantined.
        """
        for symbol in list(self._positions.keys()):
            pos = self._positions[symbol]
            if pos.status.startswith("quarantine"):
                continue
            if symbol not in valid_symbols:
                log.warning(
                    "[RiskManager] %s is NOT in current MEXC symbol list — quarantining.",
                    symbol,
                )
                self.quarantine_position(symbol, "invalid_symbol")

    def reconcile_on_startup(self, api: Any) -> None:
        """
        Called at startup after validate_positions().
        For each active position, check if the base asset has any balance on the
        exchange and if there are any open orders.  If neither is true the local
        position is a ghost — mark it needs_reconcile so it is excluded from
        the exit loop and position limits until manually reviewed.
        """
        for symbol, pos in list(self._positions.items()):
            if pos.status != "active":
                continue
            base = symbol[:-4] if symbol.endswith("USDT") else symbol
            try:
                bal = api.get_balance(base)
                total = bal["free"] + bal["locked"]
                if total <= 0:
                    open_orders = api.get_open_orders(symbol)
                    if not open_orders:
                        log.warning(
                            "[RiskManager] Startup reconcile: %s — no %s balance "
                            "(%.8f free + %.8f locked) and no open orders → needs_reconcile.",
                            symbol, base, bal["free"], bal["locked"],
                        )
                        pos.status = "needs_reconcile"
                        self.save_positions()
                    else:
                        log.info(
                            "[RiskManager] Startup reconcile: %s — no %s balance but "
                            "%d open order(s) found — keeping active.",
                            symbol, base, len(open_orders),
                        )
                else:
                    log.info(
                        "[RiskManager] Startup reconcile: %s — %s balance OK "
                        "(%.8f free + %.8f locked).",
                        symbol, base, bal["free"], bal["locked"],
                    )
            except Exception as exc:
                log.warning(
                    "[RiskManager] Startup reconcile check failed for %s: %s — leaving active.",
                    symbol, exc,
                )

    def open_position_count(self) -> int:
        return sum(1 for p in self._positions.values() if p.status == "active")

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
        Place a TP limit SELL + SL stop-limit SELL for the open position.

        MEXC does not support /api/v3/order/oco.  Two independent resting
        orders are placed instead.  If either leg fails the other is still
        placed — the bot falls back to software-only protection for the
        failed leg.  Never raises.

        Returns True when at least one order was placed successfully.
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return False

        qty       = pos.remaining_qty()
        tp_str    = self._format_price(tp_price, tick_size)
        sl_str    = self._format_price(sl_price, tick_size)
        sl_lim    = self._format_price(sl_price * 0.999, tick_size)

        tp_id = ""
        sl_id = ""

        # ── Leg 1: TP limit sell ──────────────────────────────────────
        try:
            res   = api.place_limit_sell(symbol, qty, tp_str)
            tp_id = str(res.get("orderId", ""))
            log.info("[Bracket] TP limit placed for %s @ $%s (orderId=%s)", symbol, tp_str, tp_id)
        except Exception as exc:
            log.warning("[Bracket] TP limit failed for %s: %s — software TP only", symbol, exc)

        # ── Leg 2: SL stop-limit sell ─────────────────────────────────
        try:
            res   = api.place_stop_limit_sell(symbol, qty, sl_str, sl_lim)
            sl_id = str(res.get("orderId", ""))
            log.info(
                "[Bracket] SL stop-limit placed for %s stop=$%s limit=$%s (orderId=%s)",
                symbol, sl_str, sl_lim, sl_id,
            )
        except Exception as exc:
            log.warning("[Bracket] SL stop-limit failed for %s: %s — software SL only", symbol, exc)

        pos.tp_order_id  = tp_id
        pos.sl_order_id  = sl_id
        pos.oco_tp_price = tp_price
        pos.oco_sl_price = sl_price
        self.save_positions()

        if not tp_id and not sl_id:
            log.error("[Bracket] Both legs failed for %s — software SL/TP only", symbol)
            return False
        return True

    def cancel_oco_for_position(self, symbol: str, api: Any) -> bool:
        """
        Cancel any live bracket orders (TP limit + SL stop-limit) for the position.

        IDs are cleared only when cancellation is confirmed (success response) or
        when the exchange reports the order is already gone (code -2011 / "unknown
        order").  On a genuine transport / auth failure the ID is kept so the next
        loop iteration can retry.

        Returns True  — nothing to cancel, or all cancels confirmed.
        Returns False — at least one cancel failed with a non-"already gone" error.
                        Caller MUST NOT send a market sell when False is returned with
                        active IDs, to prevent double-execution.
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return True

        all_ok = True
        for attr, label in (("tp_order_id", "TP"), ("sl_order_id", "SL")):
            order_id = getattr(pos, attr)
            if not order_id:
                continue
            try:
                api.cancel_order(symbol, order_id)
                log.info(
                    "[Bracket] Cancelled %s order for %s (orderId=%s)",
                    label, symbol, order_id,
                )
                setattr(pos, attr, "")  # clear only on confirmed success
            except Exception as exc:
                err_code = getattr(exc, "code", None)
                err_msg  = str(exc).lower()
                if err_code == -2011 or "unknown order" in err_msg:
                    # Order already filled or cancelled on exchange — safe to clear
                    log.info(
                        "[Bracket] %s order already gone for %s (orderId=%s code=%s) — clearing.",
                        label, symbol, order_id, err_code,
                    )
                    setattr(pos, attr, "")
                else:
                    log.warning(
                        "[Bracket] Cancel %s order failed for %s (orderId=%s): %s "
                        "— keeping ID, will retry.",
                        label, symbol, order_id, exc,
                    )
                    all_ok = False

        # Only zero the price tracking fields when both IDs are confirmed cleared
        if not pos.tp_order_id and not pos.sl_order_id:
            pos.oco_tp_price = 0.0
            pos.oco_sl_price = 0.0
        self.save_positions()
        return all_ok

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


# ------------------------------------------------------------------ #
#  SetupTracker                                                        #
# ------------------------------------------------------------------ #

class SetupTracker:
    """
    Persists and manages SMC/ICT setup lifecycle state.

    One active setup per symbol is enforced.  Persistence uses an atomic
    write (temp file + os.replace) so a process kill mid-write never
    corrupts setups.json.  Terminal setups are pruned after 24H on save.

    Stage 1 — infrastructure only.
    Lifecycle transitions (_monitor_setups) wired in Stage 3.
    Entry scan replacement wired in Stage 4.
    """

    def __init__(self) -> None:
        self._setups: List[Setup] = []
        self.load()

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        """Load setups from disk.  Missing file or parse error → start fresh."""
        if not _SETUPS_PATH.exists():
            self._setups = []
            return
        try:
            raw = json.loads(_SETUPS_PATH.read_text(encoding="utf-8"))
            now = time.time()
            cutoff = now - _SETUP_HISTORY_KEEP_SECS
            self._setups = [
                _setup_from_dict(d)
                for d in raw.get("setups", [])
                # Always keep active setups; keep recent terminal ones for audit
                if d.get("status") in ("IDENTIFIED", "ZONE_ENTERED")
                or float(d.get("detected_at", 0)) > cutoff
            ]
            active_count = sum(
                1 for s in self._setups
                if s.status in ("IDENTIFIED", "ZONE_ENTERED")
            )
            log.info(
                "[SetupTracker] Loaded %d setup(s) from disk (%d active).",
                len(self._setups), active_count,
            )
        except Exception as exc:
            log.warning(
                "[SetupTracker] Could not load setups.json — starting fresh: %s", exc,
            )
            self._setups = []

    def save(self) -> None:
        """
        Persist setups atomically via temp-file + os.replace.

        Prunes terminal setups older than _SETUP_HISTORY_KEEP_SECS before writing
        so both the in-memory list and the file stay bounded during long uptime.
        """
        now = time.time()
        cutoff = now - _SETUP_HISTORY_KEEP_SECS
        self._setups = [
            s for s in self._setups
            if s.status in ("IDENTIFIED", "ZONE_ENTERED")
            or s.detected_at > cutoff
        ]
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "setups":   [_setup_to_dict(s) for s in self._setups],
        }
        tmp_path = _SETUPS_PATH.with_suffix(".tmp")
        try:
            _SETUPS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, _SETUPS_PATH)
        except Exception as exc:
            log.error("[SetupTracker] Failed to save setups.json: %s", exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Queries                                                             #
    # ------------------------------------------------------------------ #

    def get_active(self, symbol: str) -> Optional[Setup]:
        """Return the IDENTIFIED or ZONE_ENTERED setup for symbol, or None."""
        for s in self._setups:
            if s.symbol == symbol and s.status in ("IDENTIFIED", "ZONE_ENTERED"):
                return s
        return None

    def has_active(self, symbol: str) -> bool:
        """
        Returns True if symbol has an active setup OR was recently confirmed.

        The CONFIRMED cooldown window (_CONFIRMED_COOLDOWN_SECS = 5 min) prevents:
          - Same-tick duplicate creation after a setup confirms.
          - DRY_RUN re-confirmation loops (no position is opened, so without
            this check the entry scan would immediately re-detect the same zone).
        """
        now = time.time()
        for s in self._setups:
            if s.symbol != symbol:
                continue
            if s.status in ("IDENTIFIED", "ZONE_ENTERED"):
                return True
            if (s.status == "CONFIRMED"
                    and s.confirmed_at > 0
                    and now - s.confirmed_at < _CONFIRMED_COOLDOWN_SECS):
                return True
        return False

    def active_setups(self) -> List[Setup]:
        """Return all setups currently in IDENTIFIED or ZONE_ENTERED state."""
        return [s for s in self._setups if s.status in ("IDENTIFIED", "ZONE_ENTERED")]

    # ------------------------------------------------------------------ #
    #  Mutations                                                           #
    # ------------------------------------------------------------------ #

    def create(
        self,
        symbol:        str,
        zone_type:     str,
        zone_high:     float,
        zone_low:      float,
        sl_price:      float,
        tp1:           float,
        tp2:           float,
        tp3:           float,
        signal_score:  float,
        atr_at_detect: float,
    ) -> Setup:
        """
        Create a new IDENTIFIED setup and persist it.
        Caller must verify has_active(symbol) == False before calling.
        """
        now = time.time()
        setup = Setup(
            symbol                  = symbol,
            status                  = "IDENTIFIED",
            zone_type               = zone_type,
            zone_high               = zone_high,
            zone_low                = zone_low,
            invalidation_price      = zone_low,
            signal_score            = signal_score,
            sl_price                = sl_price,
            tp1                     = tp1,
            tp2                     = tp2,
            tp3                     = tp3,
            atr_at_detect           = atr_at_detect,
            detected_at             = now,
            zone_entered_at         = 0.0,
            confirmed_at            = 0.0,
            expires_at              = now + _SETUP_EXPIRY_SECS,
            confirmation_expires_at = 0.0,
            invalidation_reason     = "",
        )
        self._setups.append(setup)
        self.save()
        return setup

    def transition(self, symbol: str, new_status: str, reason: str = "") -> None:
        """
        Advance an active setup to new_status and persist.

        Side-effects by target status:
          ZONE_ENTERED → sets zone_entered_at and confirmation_expires_at
          CONFIRMED    → sets confirmed_at (enables has_active() cooldown)
        No-op if no active (IDENTIFIED or ZONE_ENTERED) setup exists for symbol.
        """
        setup = self.get_active(symbol)
        if setup is None:
            return
        old_status = setup.status
        setup.status = new_status
        setup.invalidation_reason = reason

        if new_status == "ZONE_ENTERED":
            now = time.time()
            setup.zone_entered_at         = now
            setup.confirmation_expires_at = now + _CONFIRMATION_EXPIRY_SECS

        if new_status == "CONFIRMED":
            setup.confirmed_at = time.time()

        self.save()
        log.info(
            "[SetupTracker] [%s] %s → %s%s",
            symbol, old_status, new_status,
            f" ({reason})" if reason else "",
        )

    def invalidate_for_unknown_symbols(self, valid_symbols: set) -> None:
        """
        Called at startup inside the existing validate_positions() try block.
        Invalidates any active setup whose symbol is absent from the current
        MEXC listing.  Reuses the valid_symbols set already fetched for
        risk_mgr.validate_positions() — no additional API call required.
        """
        for setup in self.active_setups():
            if setup.symbol not in valid_symbols:
                self.transition(
                    setup.symbol,
                    "INVALIDATED",
                    "symbol_not_on_mexc_at_startup",
                )
                log.warning(
                    "[SetupTracker] [%s] Setup invalidated at startup: "
                    "symbol not in current MEXC listing.",
                    setup.symbol,
                )
