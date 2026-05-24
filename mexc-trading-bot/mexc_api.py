import hashlib
import hmac
import time
from urllib.parse import urlencode

import requests

from config import API_KEY, API_SECRET, BASE_URL, REQUEST_TIMEOUT


class MexcAPIError(Exception):
    pass


class MexcAPI:
    def __init__(self):
        self.api_key = API_KEY
        self.api_secret = API_SECRET
        self.session = requests.Session()
        self.session.headers.update({
            "X-MEXC-APIKEY": self.api_key,
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _timestamp(self) -> int:
        return int(time.time() * 1000)

    def _sign(self, params: dict) -> str:
        query = urlencode(params)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _request(self, method: str, endpoint: str, params: dict, signed: bool) -> dict:
        if signed:
            params["timestamp"] = self._timestamp()
            params["signature"] = self._sign(params)

        url = f"{BASE_URL}{endpoint}"
        resp = self.session.request(
            method, url, params=params, timeout=REQUEST_TIMEOUT
        )

        try:
            data = resp.json()
        except ValueError:
            raise MexcAPIError(f"Non-JSON response ({resp.status_code}): {resp.text[:200]}")

        if not resp.ok:
            code = data.get("code", resp.status_code)
            msg = data.get("msg", resp.text)
            raise MexcAPIError(f"MEXC API error {code}: {msg}")

        return data

    def _get(self, endpoint: str, params: dict | None = None, signed: bool = False) -> dict:
        return self._request("GET", endpoint, params or {}, signed)

    def _post(self, endpoint: str, params: dict | None = None) -> dict:
        return self._request("POST", endpoint, params or {}, signed=True)

    def _delete(self, endpoint: str, params: dict | None = None) -> dict:
        return self._request("DELETE", endpoint, params or {}, signed=True)

    # ------------------------------------------------------------------
    # Public endpoints (no auth required)
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            self._get("/api/v3/ping")
            return True
        except Exception:
            return False

    def get_server_time(self) -> int:
        data = self._get("/api/v3/time")
        return data["serverTime"]

    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        """
        Returns raw klines: [open_time, open, high, low, close, volume,
                             close_time, quote_volume, trades,
                             taker_buy_base, taker_buy_quote, ignore]
        """
        return self._get("/api/v3/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })

    def get_ticker_price(self, symbol: str) -> float:
        data = self._get("/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_exchange_info(self, symbol: str | None = None) -> dict:
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/api/v3/exchangeInfo", params)

    def get_symbol_filters(self, symbol: str) -> dict:
        """Returns a dict keyed by filterType for the given symbol."""
        info = self.get_exchange_info(symbol)
        for sym in info.get("symbols", []):
            if sym["symbol"] == symbol:
                return {f["filterType"]: f for f in sym.get("filters", [])}
        return {}

    def get_lot_step_size(self, symbol: str) -> float:
        filters = self.get_symbol_filters(symbol)
        step = filters.get("LOT_SIZE", {}).get("stepSize", "0.00001")
        return float(step)

    def get_min_notional(self, symbol: str) -> float:
        filters = self.get_symbol_filters(symbol)
        return float(filters.get("MIN_NOTIONAL", {}).get("minNotional", "1.0"))

    # ------------------------------------------------------------------
    # Private / signed endpoints
    # ------------------------------------------------------------------

    def get_account(self) -> dict:
        return self._get("/api/v3/account", signed=True)

    def get_balance(self, asset: str) -> float:
        """Returns free (available) balance for the given asset."""
        account = self.get_account()
        for b in account.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    def get_all_balances(self) -> dict[str, float]:
        account = self.get_account()
        return {
            b["asset"]: float(b["free"])
            for b in account.get("balances", [])
            if float(b["free"]) > 0
        }

    def get_open_orders(self, symbol: str | None = None) -> list:
        params: dict = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/api/v3/openOrders", params, signed=True)

    def get_order(self, symbol: str, order_id: int) -> dict:
        return self._get("/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def place_market_buy(self, symbol: str, quantity: float) -> dict:
        return self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": quantity,
        })

    def place_market_sell(self, symbol: str, quantity: float) -> dict:
        return self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": quantity,
        })

    def place_limit_buy(self, symbol: str, quantity: float, price: float) -> dict:
        return self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "LIMIT",
            "quantity": quantity,
            "price": price,
            "timeInForce": "GTC",
        })

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        return self._delete("/api/v3/order", {"symbol": symbol, "orderId": order_id})
