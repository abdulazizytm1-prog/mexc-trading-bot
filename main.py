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
import time
from datetime import datetime, timezone
from typing import Optional

import config
from coin_selector import CoinSelector
from market_context import MarketContextPoller
from mexc_api import MEXCAPIError, MEXCSpotAPI
from risk_manager import Position, RiskManager
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
        logging.StreamHandler(),
        logging.FileHandler("trading_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


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


def _handle_exit(
    symbol: str,
    position: Position,
    exit_reason: str,
    current_price: float,
    api: MEXCSpotAPI,
    risk_mgr: RiskManager,
) -> None:
    """
    Execute an exit order.

    Partial exits (TP1 / TP2) sell the appropriate fraction and update
    position state via risk_mgr.  Full exits (TP3 / STOP_LOSS /
    TAKE_PROFIT) close the remaining quantity and remove the position.
    """
    log.info(
        "[%s] EXIT: %s | price=%.6f | entry=%.6f",
        symbol, exit_reason, current_price, position.effective_entry,
    )

    try:
        # Cancel bracket orders before any market sell to prevent double-execution
        for oid in (getattr(position, "tp_order_id", None), getattr(position, "sl_order_id", None)):
            if oid:
                try:
                    api.cancel_order(symbol, oid)
                except MEXCAPIError:
                    pass  # already filled or cancelled — safe to ignore

        if exit_reason == "TP1":
            sell_qty = position.partial_qty(1)
            api.place_market_sell(symbol, sell_qty)
            pnl = (current_price - position.effective_entry) * sell_qty
            risk_mgr.record_closed_pnl(pnl)
            risk_mgr.handle_tp1_hit(symbol)
            log.info(
                "[%s] TP1 hit — sold %.6f (33%%) | partial PnL=%.4f USDT",
                symbol, sell_qty, pnl,
            )

        elif exit_reason == "TP2":
            sell_qty = position.partial_qty(2)
            api.place_market_sell(symbol, sell_qty)
            pnl = (current_price - position.effective_entry) * sell_qty
            risk_mgr.record_closed_pnl(pnl)
            risk_mgr.handle_tp2_hit(symbol)
            log.info(
                "[%s] TP2 hit — sold %.6f (33%%) | partial PnL=%.4f USDT | trailing active",
                symbol, sell_qty, pnl,
            )

        else:
            # TP3, STOP_LOSS, TAKE_PROFIT (legacy) → full close of remaining qty
            remaining = position.remaining_qty()
            if remaining <= 0:
                remaining = position.quantity
            api.place_market_sell(symbol, remaining)
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

    except MEXCAPIError as exc:
        log.error("[%s] Failed to execute exit (%s): %s", symbol, exit_reason, exc)


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
            score             = getattr(signal, "score", 0),
            entry_atr         = getattr(signal, "atr", 0.0),
        )
        risk_mgr.add_position(position)
        risk_mgr.place_oco_for_position(symbol)

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

    def _ensure_sym_info(symbol: str) -> dict:
        if symbol not in sym_info_cache:
            try:
                sym_info_cache[symbol] = api.get_symbol_info(symbol)
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
            #  1. Check ALL open positions for exit + trail update      #
            # -------------------------------------------------------- #
            for symbol, position in list(risk_mgr.all_positions().items()):
                try:
                    ws_price = ws_feed.get_price(symbol)
                    price    = ws_price if ws_price else api.get_ticker_price(symbol)

                    # Advance trailing stop before exit check (only moves SL up)
                    if position.trailing_active and position.entry_atr > 0:
                        risk_mgr.update_trailing_stop(symbol, price, position.entry_atr)

                    reason = risk_mgr.check_exit(symbol, price)
                    if reason:
                        # Re-fetch position after potential trail update
                        current_pos = risk_mgr.get_position(symbol)
                        if current_pos:
                            _handle_exit(symbol, current_pos, reason, price, api, risk_mgr)
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
            market_ok = check_global_market(market_ctx.get_context())
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

                    if signal.strength < 0.65:
                        log.debug(
                            "[%s] Signal strength %.2f < 0.65 — skipped.",
                            symbol, signal.strength,
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
                    cost = _handle_entry(
                        signal, balance, _ensure_sym_info(symbol), api, risk_mgr
                    )
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

    risk_mgr      = RiskManager()
    coin_selector = CoinSelector(api)

    log.info("Building initial pair list (Coinranking + MEXC cross-reference)…")
    initial_pairs = coin_selector.get_pairs()
    log.info("Trading universe (%d pairs): %s", len(initial_pairs), initial_pairs)

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
