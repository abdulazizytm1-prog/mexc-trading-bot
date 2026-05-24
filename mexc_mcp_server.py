"""
MEXC Spot Trading — MCP Server
================================
Exposes 7 tools for spot trading on MEXC via the Model Context Protocol.
Spot only — no futures, no leverage, no margin.

Setup
-----
1. Install dependencies:
       pip install mcp requests python-dotenv

2. Add credentials to .env (same file as the rest of the bot):
       MEXC_API_KEY=your_key
       MEXC_API_SECRET=your_secret

3. Register with Claude Desktop — add to claude_desktop_config.json:
       {
         "mcpServers": {
           "mexc-trading": {
             "command": "python",
             "args": ["C:/Users/ASUS/mexc-trading-bot/mexc_mcp_server.py"]
           }
         }
       }
   (Credentials are read from .env; no need to repeat them in the config.)

Tools exposed
-------------
  get_market_data   — OHLCV candles
  get_orderbook     — bids / asks
  get_ticker        — price + 24 h stats
  get_balance       — account balances  [private]
  place_order       — new spot order    [private]
  cancel_order      — cancel by ID      [private]
  get_open_orders   — list open orders  [private]

Signing rules (MEXC v3, verified)
----------------------------------
• Public endpoints  → no X-MEXC-APIKEY header, no signature
• GET  private      → signature in URL query string
• POST private      → signature in form-encoded request BODY   ← 700004 fix
• DELETE private    → signature in URL query string
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from pathlib import Path

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
# Resolve .env relative to this script, not the process working directory.
# When Claude Code spawns the MCP server, cwd is unpredictable — an absolute
# path here ensures credentials are always found.
load_dotenv(Path(__file__).parent / ".env")
_API_KEY    = os.getenv("MEXC_API_KEY", "")
_API_SECRET = os.getenv("MEXC_API_SECRET", "")
_BASE_URL   = "https://api.mexc.com"
_RECV_WIN   = 5000

# MEXC v3 valid kline intervals.  "1h" is NOT valid — use "60m".
_VALID_INTERVALS = {"1m", "5m", "15m", "30m", "60m", "4h", "1d", "1W", "1M"}

# ── MCP server instance ───────────────────────────────────────────────────────
mcp = FastMCP("MEXC Spot Trading")

# ── Shared HTTP session (no auth headers at session level) ────────────────────
_session = requests.Session()


# ─────────────────────────────────────────────────────────────────────────────
#  Internal HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sign(params: dict) -> str:
    """HMAC-SHA256 of the URL-encoded parameter string."""
    return hmac.new(
        _API_SECRET.encode(),
        urlencode(params).encode(),
        hashlib.sha256,
    ).hexdigest()


def _stamp(params: dict) -> dict:
    """Append timestamp, recvWindow, and signature in-place, return params."""
    params["timestamp"]  = int(time.time() * 1000)
    params["recvWindow"] = _RECV_WIN
    params["signature"]  = _sign(params)
    return params


def _public_get(endpoint: str, params: dict | None = None) -> Any:
    """GET with no authentication — no API-key header sent."""
    resp = _session.get(f"{_BASE_URL}{endpoint}", params=params or {}, timeout=10)
    _raise(resp)
    return resp.json()


def _private_get(endpoint: str, params: dict | None = None) -> Any:
    """GET with HMAC signature in query string."""
    p = _stamp(dict(params or {}))
    resp = _session.get(
        f"{_BASE_URL}{endpoint}",
        params=p,
        headers={"X-MEXC-APIKEY": _API_KEY},
        timeout=10,
    )
    _raise(resp)
    return resp.json()


def _private_post(endpoint: str, params: dict | None = None) -> Any:
    """POST with HMAC signature in the form-encoded body (not the URL)."""
    p = _stamp(dict(params or {}))
    resp = _session.post(
        f"{_BASE_URL}{endpoint}",
        data=p,                              # body, not params=
        headers={"X-MEXC-APIKEY": _API_KEY},
        timeout=10,
    )
    _raise(resp)
    return resp.json()


def _private_delete(endpoint: str, params: dict | None = None) -> Any:
    """DELETE with HMAC signature in query string."""
    p = _stamp(dict(params or {}))
    resp = _session.delete(
        f"{_BASE_URL}{endpoint}",
        params=p,
        headers={"X-MEXC-APIKEY": _API_KEY},
        timeout=10,
    )
    _raise(resp)
    return resp.json()


def _raise(resp: requests.Response) -> None:
    """Raise a descriptive RuntimeError on non-2xx responses."""
    if not resp.ok:
        try:
            body = resp.json()
            msg  = body.get("msg", resp.text)
            code = body.get("code", resp.status_code)
        except Exception:
            msg, code = resp.text, resp.status_code
        raise RuntimeError(f"MEXC error {code}: {msg}")


def _err(message: str) -> str:
    return json.dumps({"error": message}, indent=2)


def _ts(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _creds_ok() -> bool:
    return bool(_API_KEY and _API_SECRET)


# ─────────────────────────────────────────────────────────────────────────────
#  Tool 1 — get_market_data
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_market_data(symbol: str, interval: str = "60m", limit: int = 100) -> str:
    """
    Fetch OHLCV (candlestick) data for a symbol.

    Args:
        symbol:   Trading pair, e.g. BTCUSDT or ETHUSDT.
        interval: Candle width.  Valid values: 1m 5m 15m 30m 60m 4h 1d 1W 1M.
                  Use "60m" for hourly — MEXC does NOT accept "1h".
        limit:    Candles to return (1 – 1000, default 100).

    Returns:
        JSON with a "candles" array. Each candle has keys:
        open_time, open, high, low, close, volume, close_time, quote_volume.
    """
    if interval not in _VALID_INTERVALS:
        return _err(
            f"Invalid interval '{interval}'. "
            f"Valid values: {sorted(_VALID_INTERVALS)}. "
            "Use '60m' for hourly candles — MEXC rejects '1h'."
        )

    limit = max(1, min(int(limit), 1000))

    try:
        raw = _public_get("/api/v3/klines", {
            "symbol":   symbol.upper(),
            "interval": interval,
            "limit":    limit,
        })

        candles = [
            {
                "open_time":    _ts(row[0]),
                "open":         row[1],
                "high":         row[2],
                "low":          row[3],
                "close":        row[4],
                "volume":       row[5],
                "close_time":   _ts(row[6]),
                "quote_volume": row[7],
            }
            for row in raw
        ]

        return json.dumps({
            "symbol":   symbol.upper(),
            "interval": interval,
            "count":    len(candles),
            "candles":  candles,
        }, indent=2)

    except Exception as exc:
        log.error("get_market_data(%s): %s", symbol, exc)
        return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
#  Tool 2 — get_orderbook
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_orderbook(symbol: str, depth: int = 10) -> str:
    """
    Fetch the current order book (bids and asks) for a symbol.

    Args:
        symbol: Trading pair, e.g. BTCUSDT.
        depth:  Price levels per side.  MEXC accepts 5, 10, 20, 50, 100, 500, 1000.
                Nearest valid value is used if an unsupported number is given.

    Returns:
        JSON with "bids" and "asks" arrays. Each entry: {"price": "...", "quantity": "..."}.
        Bids are sorted highest-first; asks lowest-first.
    """
    _valid_depths = (5, 10, 20, 50, 100, 500, 1000)
    depth = min(_valid_depths, key=lambda x: abs(x - int(depth)))

    try:
        raw = _public_get("/api/v3/depth", {
            "symbol": symbol.upper(),
            "limit":  depth,
        })

        return json.dumps({
            "symbol": symbol.upper(),
            "depth":  depth,
            "bids": [{"price": b[0], "quantity": b[1]} for b in raw.get("bids", [])],
            "asks": [{"price": a[0], "quantity": a[1]} for a in raw.get("asks", [])],
        }, indent=2)

    except Exception as exc:
        log.error("get_orderbook(%s): %s", symbol, exc)
        return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
#  Tool 3 — get_ticker
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_ticker(symbol: str) -> str:
    """
    Get the current price and 24-hour rolling-window statistics for a symbol.

    Args:
        symbol: Trading pair, e.g. BTCUSDT.

    Returns:
        JSON with last_price, price_change_pct, 24h high/low, volume, bid, ask.
    """
    try:
        raw = _public_get("/api/v3/ticker/24hr", {"symbol": symbol.upper()})

        # Some MEXC gateway versions wrap a single symbol in a list
        t = raw[0] if isinstance(raw, list) else raw

        return json.dumps({
            "symbol":           t.get("symbol"),
            "last_price":       t.get("lastPrice"),
            "price_change":     t.get("priceChange"),
            "price_change_pct": t.get("priceChangePercent"),
            "high_24h":         t.get("highPrice"),
            "low_24h":          t.get("lowPrice"),
            "volume_24h":       t.get("volume"),
            "quote_volume_24h": t.get("quoteVolume"),
            "open_price":       t.get("openPrice"),
            "bid":              t.get("bidPrice"),
            "ask":              t.get("askPrice"),
            "open_time":        _ts(t.get("openTime")),
            "close_time":       _ts(t.get("closeTime")),
        }, indent=2)

    except Exception as exc:
        log.error("get_ticker(%s): %s", symbol, exc)
        return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
#  Tool 4 — get_balance
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_balance(asset: str = "") -> str:
    """
    Fetch spot account balance.  Requires API key with Read permission.

    Args:
        asset: Specific asset to query, e.g. "USDT" or "BTC".
               Leave empty to return all non-zero balances.

    Returns:
        JSON with free (available), locked (in open orders), and total for each asset.
    """
    if not _creds_ok():
        return _err("API credentials not configured. Set MEXC_API_KEY and MEXC_API_SECRET in .env.")

    try:
        data     = _private_get("/api/v3/account")
        balances = data.get("balances", [])

        def _fmt(b: dict) -> dict:
            free   = float(b.get("free",   0))
            locked = float(b.get("locked", 0))
            return {
                "asset":  b.get("asset"),
                "free":   b.get("free",   "0"),
                "locked": b.get("locked", "0"),
                "total":  f"{free + locked:.8f}",
            }

        if asset:
            target = asset.upper()
            for b in balances:
                if b.get("asset") == target:
                    return json.dumps(_fmt(b), indent=2)
            return json.dumps({"asset": target, "free": "0", "locked": "0", "total": "0"}, indent=2)

        non_zero = [_fmt(b) for b in balances
                    if float(b.get("free", 0)) > 0 or float(b.get("locked", 0)) > 0]

        return json.dumps({
            "account_type":   data.get("accountType", "SPOT"),
            "can_trade":      data.get("canTrade",    False),
            "balance_count":  len(non_zero),
            "balances":       non_zero,
        }, indent=2)

    except Exception as exc:
        log.error("get_balance: %s", exc)
        return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
#  Tool 5 — place_order
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def place_order(
    symbol:     str,
    side:       str,
    order_type: str,
    quantity:   float,
    price:      float = 0.0,
) -> str:
    """
    Place a spot buy or sell order on MEXC.
    Spot only — no futures, no leverage, no margin.

    Args:
        symbol:     Trading pair, e.g. BTCUSDT.
        side:       "BUY" or "SELL".
        order_type: "MARKET" or "LIMIT".
        quantity:   Base-asset quantity.  For BTCUSDT this is the BTC amount.
        price:      Limit price in quote currency.  Required for LIMIT orders;
                    ignored for MARKET orders.

    Returns:
        JSON with orderId, status, symbol, side, type, quantity, and price.
    """
    if not _creds_ok():
        return _err("API credentials not configured.")

    side_u = side.strip().upper()
    type_u = order_type.strip().upper()

    if side_u not in ("BUY", "SELL"):
        return _err(f"Invalid side '{side}'. Must be BUY or SELL.")
    if type_u not in ("MARKET", "LIMIT"):
        return _err(f"Invalid order_type '{order_type}'. Must be MARKET or LIMIT.")
    if quantity <= 0:
        return _err("quantity must be greater than 0.")
    if type_u == "LIMIT" and price <= 0:
        return _err("price must be greater than 0 for LIMIT orders.")

    params: dict = {
        "symbol":   symbol.upper(),
        "side":     side_u,
        "type":     type_u,
        "quantity": quantity,
    }
    if type_u == "LIMIT":
        params["price"]       = price
        params["timeInForce"] = "GTC"

    try:
        data = _private_post("/api/v3/order", params)

        return json.dumps({
            "order_id":      data.get("orderId"),
            "client_order":  data.get("clientOrderId"),
            "symbol":        data.get("symbol"),
            "side":          data.get("side"),
            "type":          data.get("type"),
            "quantity":      data.get("origQty"),
            "price":         data.get("price"),
            "status":        data.get("status"),
            "transact_time": _ts(data.get("transactTime")),
        }, indent=2)

    except Exception as exc:
        log.error("place_order(%s %s %s qty=%s): %s", side_u, type_u, symbol, quantity, exc)
        return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
#  Tool 6 — cancel_order
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def cancel_order(symbol: str, order_id: str) -> str:
    """
    Cancel an open order by its ID.

    Args:
        symbol:   Trading pair the order was placed on, e.g. BTCUSDT.
        order_id: The orderId returned by place_order or get_open_orders.

    Returns:
        JSON with the cancellation status, filled quantity, and remaining quantity.
    """
    if not _creds_ok():
        return _err("API credentials not configured.")

    try:
        data = _private_delete("/api/v3/order", {
            "symbol":  symbol.upper(),
            "orderId": order_id,
        })

        orig_qty   = float(data.get("origQty",     0))
        exec_qty   = float(data.get("executedQty", 0))

        return json.dumps({
            "order_id":        data.get("orderId"),
            "symbol":          data.get("symbol"),
            "status":          data.get("status"),
            "side":            data.get("side"),
            "type":            data.get("type"),
            "orig_quantity":   data.get("origQty"),
            "filled_quantity": data.get("executedQty"),
            "remaining":       f"{orig_qty - exec_qty:.8f}",
            "price":           data.get("price"),
        }, indent=2)

    except Exception as exc:
        log.error("cancel_order(%s, %s): %s", symbol, order_id, exc)
        return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
#  Tool 7 — get_open_orders
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_open_orders(symbol: str = "") -> str:
    """
    List all open orders, optionally filtered to a single symbol.

    Args:
        symbol: Trading pair to filter, e.g. BTCUSDT.
                Leave empty to retrieve open orders across all pairs.

    Returns:
        JSON with a count and an "orders" array. Each order includes
        order_id, symbol, side, type, quantity, filled, price, status, and time.
    """
    if not _creds_ok():
        return _err("API credentials not configured.")

    params: dict = {}
    if symbol:
        params["symbol"] = symbol.upper()

    try:
        raw = _private_get("/api/v3/openOrders", params)

        orders = []
        for o in (raw if isinstance(raw, list) else []):
            orig_qty = float(o.get("origQty",     0))
            exec_qty = float(o.get("executedQty", 0))
            orders.append({
                "order_id":        o.get("orderId"),
                "symbol":          o.get("symbol"),
                "side":            o.get("side"),
                "type":            o.get("type"),
                "quantity":        o.get("origQty"),
                "filled":          o.get("executedQty"),
                "remaining":       f"{orig_qty - exec_qty:.8f}",
                "price":           o.get("price"),
                "stop_price":      o.get("stopPrice"),
                "status":          o.get("status"),
                "time_in_force":   o.get("timeInForce"),
                "placed_at":       _ts(o.get("time")),
                "updated_at":      _ts(o.get("updateTime")),
            })

        return json.dumps({"count": len(orders), "orders": orders}, indent=2)

    except Exception as exc:
        log.error("get_open_orders(%s): %s", symbol or "all", exc)
        return _err(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _creds_ok():
        log.warning(
            "MEXC_API_KEY / MEXC_API_SECRET not set — "
            "public tools (market_data, orderbook, ticker) will work, "
            "private tools (balance, orders) will return credential errors."
        )
    log.info("Starting MEXC MCP server (spot only)…")
    mcp.run()
