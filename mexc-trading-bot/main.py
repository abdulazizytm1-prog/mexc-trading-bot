"""
MEXC Spot Trading Bot — SMC Strategy (FVG + Order Block)

Approved pairs : BTCUSDT, ETHUSDT, SOLUSDT, ADAUSDT, AVAXUSDT
Mode           : Spot only (no futures, no leverage)
"""

import logging
import math
import time

from config import KLINE_INTERVAL, KLINE_LIMIT, POLL_INTERVAL, QUOTE_ASSET, TRADING_PAIRS
from mexc_api import MexcAPI, MexcAPIError
from risk_manager import Position, RiskManager
from strategy import SMCStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _step_precision(step_size: float) -> int:
    """Decimal places implied by a LOT_SIZE stepSize value."""
    if step_size <= 0:
        return 5
    decimals = -math.floor(math.log10(step_size))
    return max(0, decimals)


def _floor_to_step(quantity: float, step_size: float) -> float:
    """Floor quantity to the nearest valid step (avoids MEXC LOT_SIZE errors)."""
    if step_size <= 0:
        return quantity
    return math.floor(quantity / step_size) * step_size


# ------------------------------------------------------------------
# Exit handler — runs for every open position on each tick
# ------------------------------------------------------------------

def manage_open_positions(
    api: MexcAPI, risk: RiskManager, usdt_balance: float
) -> None:
    for symbol in list(risk.open_symbols):
        try:
            price = api.get_ticker_price(symbol)
            reason = risk.check_exit(symbol, price)
            if reason is None:
                continue

            pos = risk.get_position(symbol)
            logger.info("Exit triggered for %s | reason=%s  price=%.6g", symbol, reason, price)

            api.place_market_sell(symbol, pos.quantity)
            pnl = risk.close_position(symbol, price)
            logger.info("Sold %s  qty=%.6f  pnl=%.4f USDT", symbol, pos.quantity, pnl)

        except MexcAPIError as exc:
            logger.error("API error managing %s: %s", symbol, exc)
        except Exception as exc:
            logger.exception("Unexpected error managing %s: %s", symbol, exc)


# ------------------------------------------------------------------
# Entry scanner — checks each approved pair for a new signal
# ------------------------------------------------------------------

def scan_for_entries(
    api: MexcAPI, strategy: SMCStrategy, risk: RiskManager, usdt_balance: float
) -> None:
    for pair in TRADING_PAIRS:
        if not risk.can_open(pair, usdt_balance):
            continue

        try:
            raw = api.get_klines(pair, KLINE_INTERVAL, KLINE_LIMIT)
            df = strategy.parse_klines(raw)
            price = api.get_ticker_price(pair)
            signal = strategy.generate_signal(pair, df, price)

            if signal is None:
                continue

            logger.info(
                "Signal  %s | entry=%.6g  SL=%.6g  TP=%.6g | %s",
                pair, signal.entry, signal.stop_loss, signal.take_profit, signal.reason,
            )

            # Size the position
            raw_qty = risk.calculate_quantity(usdt_balance, signal.entry, signal.stop_loss)
            if raw_qty <= 0:
                logger.warning("%s: calculated qty=0, skipping.", pair)
                continue

            step = api.get_lot_step_size(pair)
            qty = _floor_to_step(raw_qty, step)

            min_notional = api.get_min_notional(pair)
            if qty * signal.entry < min_notional:
                logger.warning(
                    "%s: notional %.4f < minimum %.4f, skipping.",
                    pair, qty * signal.entry, min_notional,
                )
                continue

            logger.info("Placing BUY %s  qty=%.6f  step=%.6f", pair, qty, step)
            order = api.place_market_buy(pair, qty)
            logger.info("Order accepted: %s", order)

            risk.open_position(Position(
                symbol=pair,
                entry=signal.entry,
                quantity=qty,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            ))

        except MexcAPIError as exc:
            logger.error("API error scanning %s: %s", pair, exc)
        except Exception as exc:
            logger.exception("Unexpected error scanning %s: %s", pair, exc)


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

def main() -> None:
    api = MexcAPI()
    strategy = SMCStrategy()
    risk = RiskManager()

    if not api.ping():
        logger.critical("Cannot reach MEXC API. Check connectivity and API keys.")
        return

    logger.info("Bot started — pairs: %s", TRADING_PAIRS)

    while True:
        try:
            usdt_balance = api.get_balance(QUOTE_ASSET)
            logger.info("Balance: %.4f %s | open positions: %s",
                        usdt_balance, QUOTE_ASSET, risk.open_symbols or "none")

            if risk.daily_loss_exceeded(usdt_balance):
                logger.warning("Daily loss cap active. No new trades until tomorrow.")
            else:
                manage_open_positions(api, risk, usdt_balance)
                scan_for_entries(api, strategy, risk, usdt_balance)

        except MexcAPIError as exc:
            logger.error("API error in main loop: %s", exc)
        except Exception as exc:
            logger.exception("Unexpected error in main loop: %s", exc)

        logger.info("Sleeping %ds…", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
