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
import time
from datetime import datetime
from typing import Optional

import config
from coin_selector import CoinSelector
from mexc_api import MEXCAPIError, MEXCSpotAPI
from risk_manager import Position, RiskManager
from strategy import TradeSignal, candles_to_df, generate_signal

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
            "Copy .env.example to .env and fill in MEXC_API_KEY / MEXC_API_SECRET."
        )
        return False
    return True


def _round_step(value: float, step: float, precision: int) -> float:
    """Floor value to the nearest multiple of step, then round to precision."""
    if step > 0:
        import math
        value = math.floor(value / step) * step
    return round(value, precision)


def _handle_exit(
    symbol: str,
    position: Position,
    exit_reason: str,
    current_price: float,
    api: MEXCSpotAPI,
    risk_mgr: RiskManager,
) -> None:
    log.info(
        "[%s] EXIT triggered: %s | price=%.6f | entry=%.6f",
        symbol, exit_reason, current_price, position.entry_price,
    )
    try:
        api.place_market_sell(symbol, position.quantity)
        pnl = (current_price - position.entry_price) * position.quantity
        risk_mgr.record_closed_pnl(pnl)
        risk_mgr.remove_position(symbol)
        log.info(
            "[%s] Position closed. PnL=%.4f USDT | Daily PnL=%.4f USDT (%.2f%% loss)",
            symbol, pnl, risk_mgr.daily_pnl, risk_mgr.daily_loss_pct(),
        )
    except MEXCAPIError as exc:
        log.error("[%s] Failed to close position: %s", symbol, exc)


def _handle_entry(
    signal: TradeSignal,
    balance: float,
    sym_info: dict,
    api: MEXCSpotAPI,
    risk_mgr: RiskManager,
) -> Optional[float]:
    """
    Sizes and executes a market buy.
    Returns the cost deducted from balance, or None on failure.
    """
    symbol = signal.symbol
    qty = risk_mgr.calculate_quantity(
        balance=balance,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        qty_step=sym_info["qty_step"],
        min_qty=sym_info["min_qty"],
        min_notional=sym_info["min_notional"],
    )

    if qty is None or qty <= 0:
        log.warning("[%s] Position size too small — skipped.", symbol)
        return None

    qty = _round_step(qty, sym_info["qty_step"], sym_info["base_precision"])

    try:
        order = api.place_market_buy(symbol, qty)
        order_id = str(order.get("orderId", ""))

        position = Position(
            symbol=symbol,
            side="BUY",
            entry_price=signal.entry_price,
            quantity=qty,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            order_id=order_id,
            zone_type=signal.zone_type,
        )
        risk_mgr.add_position(position)

        log.info(
            "[%s] BUY %.6f @ %.6f | SL=%.6f | TP=%.6f | zone=%s | strength=%.2f",
            symbol, qty, signal.entry_price,
            signal.stop_loss, signal.take_profit,
            signal.zone_type, signal.strength,
        )
        return qty * signal.entry_price

    except MEXCAPIError as exc:
        log.error("[%s] Order rejected: %s", symbol, exc)
        return None


# ------------------------------------------------------------------ #
#  Main loop                                                           #
# ------------------------------------------------------------------ #

def run(api: MEXCSpotAPI, risk_mgr: RiskManager, coin_selector: CoinSelector) -> None:
    # Fetch opening balance and set the daily reference
    try:
        balance = api.get_usdt_balance()
        risk_mgr.set_session_balance(balance)
        log.info("Session opening balance: %.2f USDT", balance)
    except Exception as exc:
        log.error("Cannot fetch account balance: %s", exc)
        return

    # symbol-info cache grows as new pairs are discovered after each refresh
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

            # Refresh balance at the top of every cycle
            try:
                balance = api.get_usdt_balance()
            except MEXCAPIError as exc:
                log.error("Balance fetch failed: %s", exc)
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            # -------------------------------------------------------- #
            #  1. Check ALL open positions for exit (even if pair was  #
            #     dropped from active_pairs after a refresh)           #
            # -------------------------------------------------------- #
            for symbol, position in list(risk_mgr.all_positions().items()):
                try:
                    price = api.get_ticker_price(symbol)
                    reason = risk_mgr.check_exit(symbol, price)
                    if reason:
                        _handle_exit(symbol, position, reason, price, api, risk_mgr)
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
            #  3. Scan active pairs for new entry signals               #
            # -------------------------------------------------------- #
            for symbol in active_pairs:
                if not risk_mgr.can_open_position(symbol):
                    continue

                try:
                    klines = api.get_klines(
                        symbol,
                        config.PRIMARY_TIMEFRAME,
                        config.CANDLE_LIMIT,
                    )
                    df = candles_to_df(klines)
                    if df.empty:
                        continue

                    signal = generate_signal(symbol, df)
                    if signal is None:
                        continue

                    if signal.strength < config.MIN_SIGNAL_STRENGTH:
                        log.debug(
                            "[%s] Signal too weak (%.2f < %.2f) — skipped.",
                            symbol, signal.strength, config.MIN_SIGNAL_STRENGTH,
                        )
                        continue

                    log.info(
                        "[%s] Signal: zone=%s strength=%.2f price=%.6f",
                        symbol, signal.zone_type, signal.strength, signal.entry_price,
                    )

                    cost = _handle_entry(
                        signal, balance, _ensure_sym_info(symbol), api, risk_mgr
                    )
                    if cost:
                        balance -= cost

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

    risk_mgr = RiskManager()
    coin_selector = CoinSelector(api)

    # First pair-list build happens here — fails loudly before the loop starts
    log.info("Building initial pair list (CoinGecko + MEXC cross-reference)…")
    initial_pairs = coin_selector.get_pairs()
    log.info("Trading universe (%d pairs): %s", len(initial_pairs), initial_pairs)

    run(api, risk_mgr, coin_selector)


if __name__ == "__main__":
    main()
