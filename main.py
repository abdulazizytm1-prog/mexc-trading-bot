"""
MEXC Spot Trading Bot — main entry point.

Pairs          : Dynamic — top halal USDT spot coins by MEXC volume, refreshed every 4h
Strategy       : SMC — Fair Value Gap + Order Block (long-only, spot)
Risk           : 1% per trade, 5% daily loss cap, max 3 concurrent positions

Run:
    python main.py
"""

from __future__ import annotations

import logging
import math
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import config
from coin_selector import CoinSelector
from market_context import MarketContextPoller
from mexc_api import MEXCAPIError, MEXCSpotAPI
from risk_manager import Position, RiskManager, SetupTracker
from filters import check_atr_filter, check_correlation_guard, check_global_market, check_order_book
from strategy import TradeSignal, candles_to_df, detect_kill_zone, detect_market_structure, generate_signal
from ws_price_feed import WSPriceFeed

# ------------------------------------------------------------------ #
#  Logging                                                             #
# ------------------------------------------------------------------ #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),   # stdout so Railway logs INFO as info, not error
        logging.FileHandler("trading_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# Exit-attempt cooldown: after a failed/aborted exit, wait this many seconds
# before retrying to prevent repeated sell spam on the same position.
_EXIT_COOLDOWN_SECS: int = 300
_exit_cooldown_until: Dict[str, float] = {}   # symbol → unix timestamp


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _validate_config() -> bool:
    if not config.API_KEY or not config.API_SECRET:
        log.error(
            "API credentials missing. "
            "Copy .env.example to .env and fill in MEXC_API_KEY / MEXC_SECRET."
        )
        return False
    return True


def _round_step(value: float, step: float, precision: int) -> float:
    if step > 0:
        value = math.floor(value / step) * step
    return round(value, precision)


def _is_active_session() -> bool:
    """Returns True during London (07–12 UTC) or New York (13–17 UTC) sessions."""
    if not getattr(config, "SESSION_FILTER_ENABLED", True):
        return True
    hour = datetime.now(timezone.utc).hour
    london = config.LONDON_OPEN <= hour < config.LONDON_CLOSE
    ny     = config.NY_OPEN     <= hour < config.NY_CLOSE
    return london or ny


def _compute_rsi(klines: list, period: int = 14) -> float:
    """Simple RSI from raw MEXC klines. Returns 0.0 on insufficient data."""
    closes = [float(k[4]) for k in klines]
    if len(closes) < period + 1:
        return 0.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


# ------------------------------------------------------------------ #
#  Setup lifecycle helpers (Stage 2 — defined, not yet called)        #
# ------------------------------------------------------------------ #

def _check_confirmation(symbol: str, api: MEXCSpotAPI) -> bool:
    """
    Simplified 15M MSS confirmation gate.

    Returns True when the most recently *closed* 15M candle is bullish
    AND its close exceeds the highest high of the prior three closed
    candles.  This is a structural higher-high on the trigger timeframe,
    indicating institutional defence of the OB/FVG zone.

    Uses the last 8 candles so [:-1] reliably excludes the forming bar.
    Fails silently (returns False) on any API or data error.
    """
    try:
        klines = api.get_klines(symbol, "15m", 8)
        if len(klines) < 5:
            return False
        closed      = klines[:-1]                              # drop the currently-forming candle
        prior_highs = [float(c[2]) for c in closed[-4:-1]]    # high of the 3 candles before last
        last        = closed[-1]
        close       = float(last[4])
        open_       = float(last[1])
        return close > open_ and close > max(prior_highs)
    except Exception as exc:
        log.debug("[%s] _check_confirmation failed: %s", symbol, exc)
        return False


def _monitor_setups(
    setup_tracker: SetupTracker,
    api: MEXCSpotAPI,
    risk_mgr: RiskManager,
    ws_feed: WSPriceFeed,
    sym_info_resolver,      # Callable[[str], Optional[dict]] — the _ensure_sym_info closure
    active_pairs: list,     # current coin-selector universe (SYM-1 invalidation guard)
    balance: float,
) -> None:
    """
    Advance every active setup through its lifecycle.  Called once per main-loop tick.

    Stage 2 — function is defined here but NOT yet called.
    It is wired into run() in Stage 3.

    Transition map
    --------------
    IDENTIFIED  → ZONE_ENTERED  : price enters [zone_low, zone_high]
    ZONE_ENTERED → CONFIRMED    : _check_confirmation() passes all gates (live mode)
    ZONE_ENTERED → CONFIRMED    : DRY_RUN simulated — no order placed
    *           → EXPIRED       : 8H setup timeout / 4H confirmation timeout /
                                  position limit reached at confirmation time
    *           → INVALIDATED   : price < invalidation_price /
                                  symbol dropped from active universe /
                                  symbol info unavailable at confirmation time
    """
    now = time.time()

    for setup in setup_tracker.active_setups():
        symbol = setup.symbol

        # ── Guard: active position already exists for this symbol ─────────── #
        existing = risk_mgr.get_position(symbol)
        if existing is not None and existing.status == "active":
            setup_tracker.transition(symbol, "EXPIRED", "position_already_open")
            continue

        current_price = ws_feed.get_price(symbol)
        if not current_price or current_price <= 0:
            continue   # no live price — skip this tick, try again next cycle

        # ── 1. Setup-level expiry (8H clock) ──────────────────────────────── #
        if now > setup.expires_at:
            setup_tracker.transition(symbol, "EXPIRED", "setup_8h_timeout")
            log.info("[%s] Setup expired: zone not entered within 8H.", symbol)
            continue

        # ── 2. Structural invalidation (price below OB/FVG boundary) ─────── #
        if current_price < setup.invalidation_price:
            setup_tracker.transition(
                symbol, "INVALIDATED",
                f"price_{current_price:.6f}_below_zone_{setup.invalidation_price:.6f}",
            )
            log.info(
                "[%s] Setup invalidated: price %.6f < zone_low %.6f",
                symbol, current_price, setup.invalidation_price,
            )
            continue

        # ── 3. IDENTIFIED: universe guard + zone entry check ──────────────── #
        if setup.status == "IDENTIFIED":
            if symbol not in active_pairs:
                setup_tracker.transition(symbol, "INVALIDATED", "symbol_removed_from_universe")
                log.info("[%s] Setup invalidated: symbol removed from active universe.", symbol)
                continue
            if setup.zone_low <= current_price <= setup.zone_high:
                setup_tracker.transition(symbol, "ZONE_ENTERED", "price_in_zone")
                log.info(
                    "[%s] Setup ZONE_ENTERED @ %.6f (zone %.6f-%.6f)",
                    symbol, current_price, setup.zone_low, setup.zone_high,
                )
            continue   # wait for next tick regardless of whether we just transitioned

        # ── 4. ZONE_ENTERED: universe guard + confirmation expiry + 15M MSS ─ #
        if setup.status != "ZONE_ENTERED":
            continue   # defensive: ignore any unexpected status value

        if symbol not in active_pairs:
            setup_tracker.transition(symbol, "INVALIDATED", "symbol_removed_from_universe")
            log.info("[%s] Setup invalidated: symbol removed from active universe.", symbol)
            continue

        if now > setup.confirmation_expires_at:
            setup_tracker.transition(symbol, "EXPIRED", "confirmation_4h_timeout")
            log.info(
                "[%s] Setup confirmation expired: no 15M MSS within 4H of zone entry.",
                symbol,
            )
            continue

        if not _check_confirmation(symbol, api):
            continue   # 15M MSS not yet formed — wait next tick

        # ── Confirmation received — check gates before committing ─────────── #
        # DRY_RUN: log the simulated confirm, transition for cooldown, no order.
        if config.DRY_RUN:
            setup_tracker.transition(symbol, "CONFIRMED", "dry_run_simulated")
            log.info(
                "[DRY-RUN] [%s] Setup confirmed — entry simulated "
                "(zone=%.6f-%.6f score=%.1f type=%s). No order placed.",
                symbol, setup.zone_low, setup.zone_high,
                setup.signal_score, setup.zone_type,
            )
            continue

        # Position-limit gate checked BEFORE transitioning to CONFIRMED so that
        # a limit-blocked setup uses EXPIRED (not CONFIRMED) in the audit log.
        if not risk_mgr.can_open_position(symbol):
            setup_tracker.transition(symbol, "EXPIRED", "position_limit_at_confirm")
            log.info("[%s] Setup confirmed but position limit reached — discarded.", symbol)
            continue

        si = sym_info_resolver(symbol)
        if si is None:
            setup_tracker.transition(symbol, "INVALIDATED", "symbol_info_unavailable")
            log.warning("[%s] Setup confirmed but symbol info unavailable — invalidated.", symbol)
            continue

        # All gates passed — commit status and attempt entry via existing logic.
        setup_tracker.transition(symbol, "CONFIRMED", "15m_mss_body_close")
        synthetic_signal = TradeSignal(
            symbol      = symbol,
            side        = "BUY",
            entry_price = setup.zone_high,
            stop_loss   = setup.sl_price,
            take_profit = setup.tp1,       # backward-compat required field
            zone_type   = setup.zone_type,
            strength    = round(setup.signal_score / 10.0, 3),  # required field
            score       = setup.signal_score,
            tp1         = setup.tp1,
            tp2         = setup.tp2,
            tp3         = setup.tp3,
            kill_zone   = None,
            reason      = "setup_confirmed",
            atr         = setup.atr_at_detect,
        )
        _handle_entry(synthetic_signal, balance, si, api, risk_mgr)


def _check_advanced_exits(
    symbol: str,
    position: Position,
    api: MEXCSpotAPI,
    market_ctx: MarketContextPoller,
    current_price: float,
) -> Optional[str]:
    """
    Additional safety exits beyond SL/TP:
      BTC_BEARISH — market regime is BEAR → full close (100%)
      TIME_STALL  — position open > 12H and PnL < 0 → partial close (50%)
      RSI_HIGH    — RSI(14) > 75 on primary TF → partial close (50%)
    Checked in priority order. Returns exit reason or None.
    """
    # 1. BTC / market structure BEARISH — no API call required
    ctx = market_ctx.get_context()
    if ctx is not None and getattr(ctx, "regime", "") == "BEAR":
        log.info("[%s] Market regime=BEAR — BTC_BEARISH exit triggered", symbol)
        return "BTC_BEARISH"

    # 2. Time stall: position open > 12H while underwater
    if position.open_time:
        try:
            open_dt = datetime.fromisoformat(position.open_time)
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - open_dt).total_seconds() / 3600
            pnl   = (current_price - position.effective_entry) * position.remaining_qty()
            if age_h > 12 and pnl < 0:
                log.info(
                    "[%s] TIME_STALL — open %.1fh | PnL=%.4f USDT", symbol, age_h, pnl
                )
                return "TIME_STALL"
        except Exception as exc:
            log.debug("[%s] TIME_STALL check failed: %s", symbol, exc)

    # 3. RSI overbought — requires one extra kline call
    try:
        klines = api.get_klines(symbol, config.PRIMARY_TIMEFRAME, 30)
        if klines:
            rsi = _compute_rsi(klines)
            if rsi > 75:
                log.info("[%s] RSI=%.1f > 75 — RSI_HIGH exit triggered", symbol, rsi)
                return "RSI_HIGH"
    except Exception as exc:
        log.debug("[%s] RSI check failed: %s", symbol, exc)

    return None


def _safe_market_sell(
    symbol: str,
    qty: float,
    api: MEXCSpotAPI,
    risk_mgr: RiskManager,
) -> bool:
    """
    Fetch the free base-asset balance, clamp qty to it, then place a market sell.
    Returns True on success.  Returns False (and marks position needs_reconcile)
    when the free balance is zero or the order fails — caller should set cooldown.
    """
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    try:
        bal  = api.get_balance(base)
        free = bal["free"]
    except Exception as exc:
        log.error(
            "[%s] Cannot fetch %s balance before sell: %s — aborting sell",
            symbol, base, exc,
        )
        return False

    if free <= 0:
        log.warning(
            "[%s] Free %s balance is %.8f — no sellable quantity. "
            "Marking position needs_reconcile.",
            symbol, base, free,
        )
        pos = risk_mgr.get_position(symbol)
        if pos:
            pos.status = "needs_reconcile"
            risk_mgr.save_positions()
        return False

    if qty > free:
        log.warning(
            "[%s] Requested sell qty %.8f > free balance %.8f — clamping.",
            symbol, qty, free,
        )
        qty = free

    api.place_market_sell(symbol, qty)
    return True


def _handle_exit(
    symbol: str,
    position: Position,
    exit_reason: str,
    current_price: float,
    api: MEXCSpotAPI,
    risk_mgr: RiskManager,
    tick_size: float = 0.0,
) -> bool:
    """
    Execute an exit order.  Returns True on success, False when the exit was
    aborted for safety (caller should apply exit cooldown to the symbol).

    Safety gates (checked in order):
      1. DRY_RUN — log intent, skip all orders, return True.
      2. OCO cancel confirmation — if cancel fails with live IDs, abort to
         prevent double-execution (exchange bracket + market sell).
      3. Balance check — fetch free base-asset balance before every sell;
         clamp qty or abort if balance is zero.
    """
    if config.DRY_RUN:
        log.info(
            "[DRY-RUN] Would exit %s (%s) @ %.6f | entry=%.6f",
            symbol, exit_reason, current_price, position.effective_entry,
        )
        return True

    log.info(
        "[%s] EXIT: %s | price=%.6f | entry=%.6f",
        symbol, exit_reason, current_price, position.effective_entry,
    )

    try:
        # ── Gate 1: Cancel bracket — must succeed before any market sell ──────
        had_bracket = bool(position.tp_order_id or position.sl_order_id)
        cancel_ok   = risk_mgr.cancel_oco_for_position(symbol, api)

        if not cancel_ok and had_bracket:
            log.warning(
                "[%s] OCO cancel failed with active bracket orders — aborting %s "
                "exit to prevent double-execution. Marking needs_reconcile.",
                symbol, exit_reason,
            )
            position.status = "needs_reconcile"
            risk_mgr.save_positions()
            return False

        # ── Execute per exit reason ────────────────────────────────────────────
        if exit_reason == "TP1":
            sell_qty = position.partial_qty(1)
            if not _safe_market_sell(symbol, sell_qty, api, risk_mgr):
                return False
            pnl = (current_price - position.effective_entry) * sell_qty
            risk_mgr.record_closed_pnl(pnl)
            risk_mgr.handle_tp1_hit(symbol, api, tick_size)
            log.info(
                "[%s] TP1 hit — sold %.6f (33%%) | partial PnL=%.4f USDT",
                symbol, sell_qty, pnl,
            )

        elif exit_reason == "TP2":
            sell_qty = position.partial_qty(2)
            if not _safe_market_sell(symbol, sell_qty, api, risk_mgr):
                return False
            pnl = (current_price - position.effective_entry) * sell_qty
            risk_mgr.record_closed_pnl(pnl)
            risk_mgr.handle_tp2_hit(symbol, api, tick_size)
            log.info(
                "[%s] TP2 hit — sold %.6f (33%%) | partial PnL=%.4f USDT | trailing active",
                symbol, sell_qty, pnl,
            )

        elif exit_reason == "BTC_BEARISH":
            remaining = position.remaining_qty() or position.quantity
            if not _safe_market_sell(symbol, remaining, api, risk_mgr):
                return False
            pnl = (current_price - position.effective_entry) * remaining
            risk_mgr.record_closed_pnl(pnl)
            risk_mgr.remove_position(symbol)
            log.info(
                "[%s] BTC_BEARISH — fully closed %.6f @ %.6f | PnL=%.4f USDT | "
                "Daily=%.2f%% | Weekly=%.2f%%",
                symbol, remaining, current_price, pnl,
                risk_mgr.daily_loss_pct(), risk_mgr.weekly_loss_pct(),
            )

        elif exit_reason in ("RSI_HIGH", "TIME_STALL"):
            remaining = position.remaining_qty() or position.quantity
            sell_qty  = round(remaining * 0.50, 8)
            if sell_qty > 0:
                if not _safe_market_sell(symbol, sell_qty, api, risk_mgr):
                    return False
                pnl = (current_price - position.effective_entry) * sell_qty
                risk_mgr.record_closed_pnl(pnl)
                live_pos = risk_mgr.get_position(symbol)
                if live_pos and live_pos.status == "active":
                    live_pos.quantity = max(round(live_pos.quantity - sell_qty, 8), 0.0)
                    risk_mgr.save_positions()
                log.info(
                    "[%s] %s — sold 50%% (%.6f) @ %.6f | PnL=%.4f USDT",
                    symbol, exit_reason, sell_qty, current_price, pnl,
                )

        else:
            # TP3, STOP_LOSS, TAKE_PROFIT (legacy) — full close of remaining qty
            remaining = position.remaining_qty() or position.quantity
            if not _safe_market_sell(symbol, remaining, api, risk_mgr):
                return False
            pnl = (current_price - position.effective_entry) * remaining
            risk_mgr.record_closed_pnl(pnl)
            risk_mgr.remove_position(symbol)
            log.info(
                "[%s] Position fully closed (%s). PnL=%.4f USDT | "
                "Daily=%.4f USDT (%.2f%%) | Weekly=%.2f%%",
                symbol, exit_reason, pnl,
                risk_mgr.daily_pnl, risk_mgr.daily_loss_pct(),
                risk_mgr.weekly_loss_pct(),
            )

        return True

    except MEXCAPIError as exc:
        log.error("[%s] Failed to execute exit (%s): %s", symbol, exit_reason, exc)
        return False


def _handle_entry(
    signal: TradeSignal,
    balance: float,
    sym_info: dict,
    api: MEXCSpotAPI,
    risk_mgr: RiskManager,
) -> Optional[float]:
    """
    Sizes and executes a market buy.
    Always fetches fresh balance from MEXC immediately before sizing so the
    position percentage is based on the real current equity, not a stale value.
    Returns the cost deducted from balance, or None on failure.
    """
    symbol     = signal.symbol
    is_friday  = signal.kill_zone == "FRIDAY_REDUCED"

    if config.DRY_RUN:
        log.info(
            "[DRY-RUN] Would BUY %s @ %.6f | SL=%.6f | score=%d/10 | zone=%s",
            symbol, signal.entry_price, signal.stop_loss,
            getattr(signal, "score", 0), signal.zone_type,
        )
        return None

    # Fresh balance for accurate percentage-based sizing
    try:
        balance = api.get_usdt_balance()
    except MEXCAPIError as exc:
        log.error("[%s] Cannot fetch fresh balance for sizing: %s", symbol, exc)
        return None

    qty = risk_mgr.calculate_quantity(
        balance=balance,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        qty_step=sym_info["qty_step"],
        min_qty=sym_info["min_qty"],
        min_notional=sym_info["min_notional"],
        is_friday=is_friday,
    )

    if qty is None or qty <= 0:
        log.warning("[%s] Position size too small — skipped.", symbol)
        return None

    qty = _round_step(qty, sym_info["qty_step"], sym_info["base_precision"])

    try:
        order    = api.place_market_buy(symbol, qty)
        order_id = str(order.get("orderId", ""))

        executed_qty = float(order.get("executedQty")         or qty)
        cumul_quote  = float(order.get("cummulativeQuoteQty") or 0)
        fill_price   = (
            cumul_quote / executed_qty
            if executed_qty > 0 and cumul_quote > 0
            else signal.entry_price
        )

        # Enforce hard SL cap: never more than 3% from fill price
        max_sl_dist = fill_price * 0.03
        safe_sl     = max(signal.stop_loss, fill_price - max_sl_dist, 0.0)
        risk        = fill_price - safe_sl

        # Recompute TPs from actual fill price if fill differs from signal price
        if risk > 0 and abs(fill_price - signal.entry_price) / signal.entry_price > 0.001:
            tp1 = fill_price + risk * 1.0
            tp2 = fill_price + risk * 2.0
            tp3 = fill_price + risk * 3.0
        else:
            tp1 = signal.tp1 if signal.tp1 > 0 else fill_price + risk * 1.0
            tp2 = signal.tp2 if signal.tp2 > 0 else fill_price + risk * 2.0
            tp3 = signal.tp3 if signal.tp3 > 0 else fill_price + risk * 3.0

        position = Position(
            symbol            = symbol,
            side              = "BUY",
            entry_price       = fill_price,
            quantity          = executed_qty,
            stop_loss         = safe_sl,
            take_profit       = tp1,
            order_id          = order_id,
            zone_type         = signal.zone_type,
            fill_price        = fill_price,
            tp1               = tp1,
            tp2               = tp2,
            tp3               = tp3,
            open_time         = datetime.now(timezone.utc).isoformat(timespec="seconds"),
            score             = int(getattr(signal, "score", 0)),
            entry_atr         = getattr(signal, "atr", 0.0),
        )
        risk_mgr.add_position(position)
        risk_mgr.place_oco_for_position(
            symbol, api, tp1, safe_sl, sym_info.get("tick_size", 0.0)
        )

        log.info(
            "[%s] BUY %.6f @ %.6f (fill) | SL=%.6f | TP1=%.6f TP2=%.6f TP3=%.6f "
            "| zone=%s | score=%d/10 | friday=%s | %s",
            symbol, executed_qty, fill_price,
            safe_sl, tp1, tp2, tp3,
            signal.zone_type, getattr(signal, "score", 0),
            is_friday, getattr(signal, "reason", ""),
        )
        return executed_qty * fill_price

    except MEXCAPIError as exc:
        log.error("[%s] Order rejected: %s", symbol, exc)
        return None


# ------------------------------------------------------------------ #
#  Main loop                                                           #
# ------------------------------------------------------------------ #

def run(
    api: MEXCSpotAPI,
    risk_mgr: RiskManager,
    coin_selector: CoinSelector,
    market_ctx: MarketContextPoller,
    ws_feed: WSPriceFeed,
) -> None:
    # Fetch opening balance and set the daily reference
    try:
        balance = api.get_usdt_balance()
        risk_mgr.set_session_balance(balance)
        log.info("Session opening balance: %.2f USDT", balance)
    except Exception as exc:
        log.error("Cannot fetch account balance: %s", exc)
        return

    sym_info_cache: dict = {}

    def _ensure_sym_info(symbol: str) -> Optional[dict]:
        """Returns symbol metadata dict, or None if the symbol is invalid/delisted."""
        if symbol not in sym_info_cache:
            try:
                sym_info_cache[symbol] = api.get_symbol_info(symbol)
            except MEXCAPIError as exc:
                if exc.code == -1121 or "not found" in str(exc).lower():
                    log.error(
                        "[%s] Symbol not found on MEXC — quarantining position.", symbol,
                    )
                    risk_mgr.quarantine_position(symbol, "invalid_symbol")
                    sym_info_cache[symbol] = None
                else:
                    log.warning("Could not fetch symbol info for %s: %s", symbol, exc)
                    sym_info_cache[symbol] = {
                        "base_precision": 6, "quote_precision": 2,
                        "min_qty": 0.0, "qty_step": 0.0,
                        "min_notional": 5.0, "tick_size": 0.0,
                    }
            except Exception as exc:
                log.warning("Could not fetch symbol info for %s: %s", symbol, exc)
                sym_info_cache[symbol] = {
                    "base_precision": 6, "quote_precision": 2,
                    "min_qty": 0.0, "qty_step": 0.0,
                    "min_notional": 5.0, "tick_size": 0.0,
                }
        return sym_info_cache[symbol]

    while True:
        try:
            loop_start = time.time()

            # ── Dynamic pair list (auto-refreshes every 4 h) ─────────
            active_pairs = coin_selector.get_pairs()

            # After a coin selector refresh, update the WS uuid map
            uuid_map = {
                c.mexc_symbol: c.uuid
                for c in (coin_selector._coins or [])
                if c.uuid
            }
            if uuid_map:
                ws_feed.update_uuid_map(uuid_map)

            # Refresh balance at the top of every cycle
            try:
                balance = api.get_usdt_balance()
            except MEXCAPIError as exc:
                log.error("Balance fetch failed: %s", exc)
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            # -------------------------------------------------------- #
            #  1. Check ACTIVE positions for exit + trail update        #
            # -------------------------------------------------------- #
            for symbol, position in list(risk_mgr.active_positions().items()):
                # Skip if within post-failure cooldown window
                if _exit_cooldown_until.get(symbol, 0.0) > time.time():
                    log.debug(
                        "[%s] Exit cooldown active (%.0fs remaining) — skipping.",
                        symbol,
                        _exit_cooldown_until[symbol] - time.time(),
                    )
                    continue
                try:
                    ws_price = ws_feed.get_price(symbol)
                    price    = ws_price if ws_price else api.get_ticker_price(symbol)

                    # Advance trailing stop before exit check (only moves SL up)
                    if position.trailing_active and position.entry_atr > 0:
                        risk_mgr.update_trailing_stop(symbol, price, position.entry_atr)

                    reason = risk_mgr.check_exit(symbol, price)
                    if not reason:
                        reason = _check_advanced_exits(symbol, position, api, market_ctx, price)
                    if reason:
                        current_pos = risk_mgr.get_position(symbol)
                        if current_pos:
                            si = _ensure_sym_info(symbol)
                            if si is None:
                                # Symbol quarantined mid-loop — skip this cycle
                                continue
                            exit_ok = _handle_exit(
                                symbol, current_pos, reason, price, api, risk_mgr,
                                si.get("tick_size", 0.0),
                            )
                            if not exit_ok:
                                _exit_cooldown_until[symbol] = (
                                    time.time() + _EXIT_COOLDOWN_SECS
                                )
                                log.warning(
                                    "[%s] Exit aborted — cooldown set for %ds.",
                                    symbol, _EXIT_COOLDOWN_SECS,
                                )
                except MEXCAPIError as exc:
                    log.error("[%s] Error checking exit: %s", symbol, exc)

            # -------------------------------------------------------- #
            #  2. Daily loss cap check                                  #
            # -------------------------------------------------------- #
            if risk_mgr.daily_loss_cap_reached():
                log.warning(
                    "Daily loss cap reached (%.2f%%). No new entries today.",
                    risk_mgr.daily_loss_pct(),
                )
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            # -------------------------------------------------------- #
            #  3. Global market context gates                           #
            # -------------------------------------------------------- #
            if not market_ctx.is_safe_to_trade():
                log.warning(
                    "[MarketContext] Global market down > %.1f%% — no new entries.",
                    abs(getattr(config, "MARKET_DROP_NO_TRADE_PCT", -3.0)),
                )
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            altcoin_restricted = market_ctx.is_altcoin_restricted()
            if altcoin_restricted:
                ctx = market_ctx.get_context()
                log.info(
                    "[MarketContext] BTC dominance %.1f%% > %.1f%% — altcoin entries restricted.",
                    ctx.btc_dominance if ctx else 0.0,
                    getattr(config, "BTC_DOM_ALTCOIN_RESTRICT", 55.0),
                )

            # -------------------------------------------------------- #
            #  4. Session filter                                        #
            # -------------------------------------------------------- #
            if not _is_active_session():
                log.debug("Outside London/NY session — skipping new entries.")
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            # -------------------------------------------------------- #
            #  5. Scan active pairs for new entry signals               #
            # -------------------------------------------------------- #

            # ── 5a. Kill zone gate — no entries outside London/NY/London-close ─
            kill_zone = detect_kill_zone()
            if kill_zone is None:
                log.debug("No active kill zone — skipping entry scan.")
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            # ── 5b. Global market filter (btc_dom, crash, greed, bias) ─────────
            _tf = getattr(config, "PRIMARY_TIMEFRAME", "60m").lower()
            _trade_type = "swing" if _tf in ("4h", "1d") else "daytrading"
            market_ok = check_global_market(market_ctx.get_context(), _trade_type)
            if not market_ok["tradeable"]:
                log.info("[MarketFilter] %s", market_ok["reason"])
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            # ── 5c. Global position limits — don't even scan if caps hit ────────
            if not risk_mgr.can_open_position(""):
                log.info("[RiskMgr] Global position limits reached — pausing entry scan.")
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            entries_this_cycle = 0
            max_entries = 1 if altcoin_restricted else 2

            for symbol in active_pairs:
                if entries_this_cycle >= max_entries:
                    break

                # Coin quality gate
                coin_score = coin_selector.get_quality_score(symbol)
                if coin_score < config.MIN_COIN_SCORE:
                    log.debug(
                        "[%s] Coin quality too low (%d/10) — skipped.",
                        symbol, coin_score,
                    )
                    continue

                # DEX spike gate (real-time)
                if ws_feed.is_dex_spike(symbol):
                    log.warning(
                        "[%s] DEX volume spike (%.0f%%) — skipped.",
                        symbol, ws_feed.get_dex_ratio(symbol) * 100,
                    )
                    continue

                try:
                    klines = api.get_klines(
                        symbol,
                        config.PRIMARY_TIMEFRAME,
                        config.CANDLE_LIMIT,
                    )
                    if not klines:
                        continue

                    ws_price    = ws_feed.get_price(symbol)
                    current_price = ws_price or float(klines[-1][4])

                    # ── a. ATR volatility filter ─────────────────────────────────
                    atr_check = check_atr_filter(klines, current_price)
                    if not atr_check["tradeable"]:
                        log.debug("[%s] ATR filter: %s", symbol, atr_check["reason"])
                        continue

                    # ── b. Correlation guard ─────────────────────────────────────
                    corr_check = check_correlation_guard(symbol, risk_mgr.all_positions())
                    if not corr_check["allowed"]:
                        log.info("[%s] Correlation guard: %s", symbol, corr_check["reason"])
                        continue

                    # ── c. Per-symbol position / daily-trade check ────────────────
                    if not risk_mgr.can_open_position(symbol):
                        continue

                    # ── d. Order book liquidity ──────────────────────────────────
                    ob_check = check_order_book(symbol, current_price, api)
                    if not ob_check["liquid_enough"]:
                        log.info("[%s] Order book: %s", symbol, ob_check["reason"])
                        continue

                    # ── e. 4H market structure — BULLISH bias required ────────────
                    htf_df = None
                    try:
                        htf_klines = api.get_klines(
                            symbol,
                            getattr(config, "HTF_TIMEFRAME", "4h"),
                            getattr(config, "HTF_CANDLE_LIMIT", 50),
                        )
                        htf_df = candles_to_df(htf_klines) if htf_klines else None
                    except Exception:
                        pass  # non-fatal; signal falls back to primary TF

                    if htf_df is not None and not htf_df.empty:
                        structure = detect_market_structure(htf_df)
                        if structure.get("bias") != "BULLISH":
                            log.debug(
                                "[%s] HTF bias=%s (not BULLISH) — skipped.",
                                symbol, structure.get("bias", "UNKNOWN"),
                            )
                            continue

                    # ── f. Signal generation ─────────────────────────────────────
                    df = candles_to_df(klines)
                    if df.empty:
                        continue

                    signal = generate_signal(symbol, df, htf_df=htf_df)
                    if signal is None:
                        continue

                    if signal.score < config.EXECUTE_SCORE:
                        log.debug(
                            "[%s] Signal score %.2f < %.1f — skipped.",
                            symbol, signal.score, config.EXECUTE_SCORE,
                        )
                        continue

                    log.info(
                        "[%s] Signal: zone=%s score=%d/10 price=%.6f "
                        "SL=%.6f TP1=%.6f TP2=%.6f TP3=%.6f "
                        "coin=%d/10 kill=%s atr=%.2f%% reason=%s",
                        symbol, signal.zone_type, signal.score, signal.entry_price,
                        signal.stop_loss, signal.tp1, signal.tp2, signal.tp3,
                        coin_score, signal.kill_zone,
                        atr_check["atr_pct"], signal.reason,
                    )

                    # ── g. Execute ───────────────────────────────────────────────
                    si = _ensure_sym_info(symbol)
                    if si is None:
                        continue
                    cost = _handle_entry(signal, balance, si, api, risk_mgr)
                    if cost:
                        balance -= cost
                        entries_this_cycle += 1

                except MEXCAPIError as exc:
                    log.error("[%s] API error during scan: %s", symbol, exc)
                except Exception as exc:
                    log.exception("[%s] Unexpected error during scan: %s", symbol, exc)

            elapsed = time.time() - loop_start
            sleep_for = max(0.0, config.LOOP_INTERVAL_SECONDS - elapsed)
            log.debug(
                "Loop done in %.1fs | pairs=%d | positions=%d | sleeping %.1fs",
                elapsed, len(active_pairs), risk_mgr.open_position_count(), sleep_for,
            )
            time.sleep(sleep_for)

        except KeyboardInterrupt:
            log.info("Shutdown requested — closing gracefully.")
            ws_feed.stop()
            market_ctx.stop()
            break
        except Exception as exc:
            log.exception("Unhandled exception in main loop: %s", exc)
            time.sleep(10)


def main() -> None:
    if not _validate_config():
        return

    api = MEXCSpotAPI()

    try:
        server_time = api.get_server_time()
        log.info(
            "Connected to MEXC. Server time: %s",
            datetime.utcfromtimestamp(server_time / 1000).strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
    except Exception as exc:
        log.error("MEXC connectivity check failed: %s", exc)
        return

    if config.DRY_RUN:
        log.info(
            "*** DRY-RUN MODE — no real orders will be placed. "
            "Set LIVE_TRADING=true in .env to enable live execution. ***"
        )
    else:
        log.warning(
            "*** LIVE TRADING ENABLED — real orders will be placed on MEXC. ***"
        )

    risk_mgr      = RiskManager()
    coin_selector = CoinSelector(api)

    log.info("Building initial pair list (Coinranking + MEXC cross-reference)…")
    initial_pairs = coin_selector.get_pairs()
    log.info("Trading universe (%d pairs): %s", len(initial_pairs), initial_pairs)

    # ── PATCH 2: Validate persisted positions against current MEXC symbol list ──
    try:
        valid_mexc_symbols = api.get_all_usdt_spot_symbols()
        risk_mgr.validate_positions(valid_mexc_symbols)
    except Exception as exc:
        log.warning("Startup symbol validation skipped (MEXC fetch failed): %s", exc)

    # ── PATCH 3: Reconcile local positions with real exchange balances ──────────
    try:
        risk_mgr.reconcile_on_startup(api)
    except Exception as exc:
        log.warning("Startup reconcile failed: %s", exc)

    # Build uuid_map from the first refresh
    uuid_map = {
        c.mexc_symbol: c.uuid
        for c in (coin_selector._coins or [])
        if c.uuid
    }

    # Start background services
    market_ctx = MarketContextPoller()
    market_ctx.start()

    ws_feed = WSPriceFeed(uuid_map)
    ws_feed.start()

    run(api, risk_mgr, coin_selector, market_ctx, ws_feed)


if __name__ == "__main__":
    main()
