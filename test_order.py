"""
Quick smoke-test: place a ~$1 BTCUSDT market buy then immediately sell it back.
Run: python test_order.py
"""
import logging, sys
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from mexc_api import MEXCSpotAPI, MEXCAPIError

api = MEXCSpotAPI()

# ── 1. Price check ─────────────────────────────────────────────────────
price = api.get_ticker_price("BTCUSDT")
print(f"BTCUSDT price: ${price:,.2f}")

# $1 worth — MEXC minimum notional is usually $5, use $6 to be safe
spend_usdt = 6.0
qty = round(spend_usdt / price, 6)
print(f"Buying {qty} BTC (~${spend_usdt})")

# ── 2. Market buy ──────────────────────────────────────────────────────
try:
    buy = api.place_market_buy("BTCUSDT", qty)
    print(f"BUY OK  orderId={buy.get('orderId')}  status={buy.get('status')}")
    print(f"        executedQty={buy.get('executedQty')}  cummulativeQuoteQty={buy.get('cummulativeQuoteQty')}")
except MEXCAPIError as e:
    print(f"BUY FAILED: {e}")
    sys.exit(1)

# ── 3. Market sell (same qty) ──────────────────────────────────────────
try:
    sell = api.place_market_sell("BTCUSDT", qty)
    print(f"SELL OK orderId={sell.get('orderId')}  status={sell.get('status')}")
except MEXCAPIError as e:
    print(f"SELL FAILED: {e}")
    sys.exit(1)

print("\nTest passed — POST signing and Content-Type are correct.")
