import hmac
import hashlib
import logging
import time
import requests
from urllib.parse import urlencode
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger(__name__)

# Dedicated file logger for API errors — appends to error.log.
# Configured once at module import; safe to call from any thread.
_err_log = logging.getLogger(f"{__name__}.errors")
if not _err_log.handlers:
    _efh = logging.FileHandler("error.log", encoding="utf-8")
    _efh.setLevel(logging.WARNING)
    _efh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _err_log.addHandler(_efh)
    _err_log.propagate = True   # also flows through root → trading_bot.log

from config import API_KEY, API_SECRET, BASE_URL, RECV_WINDOW


class MEXCAPIError(Exception):
    def __init__(self, status_code: int, code: int, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"MEXC API error {code}: {message} (HTTP {status_code})")


class MEXCSpotAPI:
    """
    Thin wrapper around the MEXC v3 REST API — spot trading only.
    Handles HMAC-SHA256 signing, retries, and error normalisation.
    """

    MAX_RETRIES = 3
    RETRY_BACKOFF = 2  # seconds, doubled on each retry

    def __init__(self):
        self.api_key = API_KEY
        self.api_secret = API_SECRET
        self.session = requests.Session()
        # No auth headers on the session.
        #
        # Sending X-MEXC-APIKEY on a public endpoint (klines, ticker, depth …)
        # causes MEXC to enter authentication-check mode.  If no timestamp +
        # signature is present it returns error 700004 — even though the
        # endpoint is entirely public.  The header is therefore injected
        # per-request only when `auth=True` (private/signed calls).
        #
        # No Content-Type on the session: all params go in the query string
        # for every method (GET, POST, DELETE) — no body is ever sent.

    # ------------------------------------------------------------------ #
    #  Signing                                                             #
    # ------------------------------------------------------------------ #

    def _sign(self, query_string: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _build_signed_params(self, params: Dict) -> Dict:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = RECV_WINDOW
        qs = urlencode(params)
        params["signature"] = self._sign(qs)
        return params

    # ------------------------------------------------------------------ #
    #  Transport                                                           #
    # ------------------------------------------------------------------ #

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        query_params: Optional[Dict] = None,
        body_params: Optional[Dict] = None,
        body_str: Optional[str] = None,
        auth: bool = False,
    ) -> Any:
        """
        Single retry-aware transport layer.

        Public endpoints  (auth=False) — no API key header, no signature.
        Private endpoints (auth=True)  — X-MEXC-APIKEY header added here.

        Param routing:
          GET / DELETE → signed params in URL query string  (query_params)
          POST         → signed params in form-encoded body (body_str)

        MEXC v3 uses query-string params for ALL methods (GET, POST, DELETE).
        Sending params in a form-encoded body returns 700013 ("Invalid content
        Type") regardless of the Content-Type header.  The body_params and
        body_str arguments are kept for potential future use but are NOT used
        by any current MEXC endpoint — everything goes through query_params.
        """
        url = f"{BASE_URL}{endpoint}"
        headers = {"X-MEXC-APIKEY": self.api_key} if auth else {}

        # POST body must be explicitly form-encoded; passing a dict lets
        # requests choose the encoding, which can trigger MEXC error 700013.
        if body_str is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        last_exc: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=query_params or None,           # → URL query string
                    data=body_str or body_params or None,  # → form-encoded body
                    headers=headers,
                    timeout=5,
                )

                if resp.status_code == 429:
                    _err_log.warning(
                        "429 rate-limit: %s %s (attempt %d/%d) — sleeping 60s",
                        method, endpoint, attempt + 1, self.MAX_RETRIES,
                    )
                    time.sleep(60)
                    continue

                data = resp.json()

                if not resp.ok:
                    exc = MEXCAPIError(
                        resp.status_code,
                        data.get("code", -1),
                        data.get("msg", resp.text),
                    )
                    _err_log.error(
                        "%s %s → HTTP %d code=%s msg=%s",
                        method, endpoint, resp.status_code,
                        data.get("code", -1), data.get("msg", resp.text),
                    )
                    raise exc

                return data

            except MEXCAPIError:
                raise   # already logged; propagate immediately

            except requests.Timeout as exc:
                _err_log.warning(
                    "Timeout: %s %s (attempt %d/%d) — retrying in 5s",
                    method, endpoint, attempt + 1, self.MAX_RETRIES,
                )
                last_exc = exc
                time.sleep(5)

            except requests.ConnectionError as exc:
                wait = self.RETRY_BACKOFF * (2 ** attempt)
                _err_log.warning(
                    "ConnectionError: %s %s (attempt %d/%d) — retrying in %.0fs",
                    method, endpoint, attempt + 1, self.MAX_RETRIES, wait,
                )
                last_exc = exc
                time.sleep(wait)

            except Exception as exc:
                _err_log.error(
                    "Unexpected error: %s %s (attempt %d/%d): %s",
                    method, endpoint, attempt + 1, self.MAX_RETRIES, exc,
                )
                last_exc = exc
                time.sleep(self.RETRY_BACKOFF * (2 ** attempt))

        _err_log.error(
            "Max retries (%d) exceeded for %s %s — last error: %s",
            self.MAX_RETRIES, method, endpoint, last_exc,
        )
        raise MEXCAPIError(-1, -1, f"Max retries exceeded for {endpoint}: {last_exc}")

    def _get(self, endpoint: str, params: Optional[Dict] = None, signed: bool = False) -> Any:
        params = dict(params or {})
        if signed:
            params = self._build_signed_params(params)
        # auth=signed: public GETs send no API key header at all
        return self._request("GET", endpoint, query_params=params, auth=signed)

    def _post(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        # MEXC v3 POST endpoints require signed params in the URL query string.
        # Sending a form-encoded body (data=) returns 700013 ("Invalid content
        # Type") regardless of the Content-Type header set.  Passing params as
        # the query string (same as GET/DELETE) is the correct approach.
        params = self._build_signed_params(dict(params or {}))
        return self._request("POST", endpoint, query_params=params, auth=True)

    def _delete(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        # DELETE uses the query string on MEXC (same as Binance v3).
        params = self._build_signed_params(dict(params or {}))
        return self._request("DELETE", endpoint, query_params=params, auth=True)

    # ------------------------------------------------------------------ #
    #  Public endpoints (no auth)                                          #
    # ------------------------------------------------------------------ #

    def get_server_time(self) -> int:
        """Returns server time in milliseconds."""
        return self._get("/api/v3/time")["serverTime"]

    def get_exchange_info(self, symbol: Optional[str] = None) -> Dict:
        params = {"symbol": symbol} if symbol else {}
        return self._get("/api/v3/exchangeInfo", params)

    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> List:
        """
        Returns raw kline list.
        Each element: [open_time, open, high, low, close, volume, close_time,
                       quote_vol, trade_count, taker_buy_vol, taker_buy_quote_vol, ignore]
        """
        return self._get("/api/v3/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })

    def get_ticker_price(self, symbol: str) -> float:
        return float(self._get("/api/v3/ticker/price", {"symbol": symbol})["price"])

    def get_order_book(self, symbol: str, limit: int = 5) -> Dict:
        return self._get("/api/v3/depth", {"symbol": symbol, "limit": limit})

    # ------------------------------------------------------------------ #
    #  Private endpoints (signed)                                          #
    # ------------------------------------------------------------------ #

    def get_account_info(self) -> Dict:
        return self._get("/api/v3/account", signed=True)

    def get_balance(self, asset: str) -> Dict[str, float]:
        for b in self.get_account_info().get("balances", []):
            if b["asset"] == asset:
                return {"free": float(b["free"]), "locked": float(b["locked"])}
        return {"free": 0.0, "locked": 0.0}

    def get_usdt_balance(self) -> float:
        return self.get_balance("USDT")["free"]

    def get_asset_balance(self, coin: str) -> float:
        return self.get_balance(coin)["free"]

    # ------------------------------------------------------------------ #
    #  Order management                                                    #
    # ------------------------------------------------------------------ #

    def place_market_buy(self, symbol: str, quantity: float) -> Dict:
        return self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": quantity,
        })

    def place_market_sell(self, symbol: str, quantity: float) -> Dict:
        return self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": quantity,
        })

    def place_limit_buy(self, symbol: str, quantity: float, price: float) -> Dict:
        return self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "LIMIT",
            "quantity": quantity,
            "price": price,
            "timeInForce": "GTC",
        })

    def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        return self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "LIMIT",
            "quantity": quantity,
            "price": price,
            "timeInForce": "GTC",
        })

    def cancel_order(self, symbol: str, order_id: str) -> Dict:
        return self._delete("/api/v3/order", {"symbol": symbol, "orderId": order_id})

    # ── Exchange-side bracket orders (TP limit + SL stop-limit) ─────
    # MEXC does not support /api/v3/order/oco.  We place two independent
    # resting orders instead: a LIMIT SELL for the take-profit and a
    # STOP_LOSS_LIMIT SELL for the stop-loss.  Both are cancelled before
    # any software-side exit to prevent double-execution.

    def place_stop_limit_sell(
        self,
        symbol:      str,
        quantity:    float,
        stop_price:  str,   # trigger price (pre-formatted string)
        limit_price: str,   # fill price, typically stop_price * 0.999
    ) -> Dict:
        """
        Place a STOP_LOSS_LIMIT SELL order.

        stop_price  — price at which the order is triggered
        limit_price — limit price after trigger (set 0.1% below stop_price
                      to absorb slippage while keeping a near-market fill)

        Raises MEXCAPIError on exchange rejection.
        """
        return self._post("/api/v3/order", {
            "symbol":      symbol,
            "side":        "SELL",
            "type":        "STOP_LOSS_LIMIT",
            "quantity":    quantity,
            "stopPrice":   stop_price,
            "price":       limit_price,
            "timeInForce": "GTC",
        })

    def cancel_all_orders(self, symbol: str) -> List:
        return self._delete("/api/v3/openOrders", {"symbol": symbol})

    def get_order(self, symbol: str, order_id: str) -> Dict:
        return self._get("/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def get_open_orders(self, symbol: Optional[str] = None) -> List:
        params = {"symbol": symbol} if symbol else {}
        return self._get("/api/v3/openOrders", params, signed=True)

    # ------------------------------------------------------------------ #
    #  Market universe helpers (used by CoinSelector)                     #
    # ------------------------------------------------------------------ #

    # MEXC valid kline intervals (v3 API — differs from Binance naming):
    #   1m  5m  15m  30m  60m  4h  1d  1W  1M
    # Note: MEXC uses "60m", NOT "1h".  Sending "1h" returns error -1121.
    VALID_INTERVALS = {"1m", "5m", "15m", "30m", "60m", "4h", "1d", "1W", "1M"}

    # Status values MEXC has been observed to use for active/tradeable pairs.
    # Run debug_exchange_info.py to confirm which one your MEXC instance uses.
    _ACTIVE_STATUSES: Set = {"ENABLED", "TRADING", "1", 1, True}

    @staticmethod
    def _extract_symbols_list(info: Any) -> List[Dict]:
        """
        Find the symbols array regardless of where it lives in the response.

        MEXC has been observed to nest it under:
          • info["symbols"]  — standard v3 layout
          • info["data"]     — some MEXC gateway versions wrap in a data key
          • info (root)      — if the endpoint returns a bare list

        Logs the key chosen and top-level keys for debugging.
        """
        if isinstance(info, list):
            log.debug("exchangeInfo: response is a bare list (%d items)", len(info))
            return info

        if isinstance(info, dict):
            log.debug("exchangeInfo: top-level keys = %s", list(info.keys()))
            for candidate in ("symbols", "data", "result", "list", "items"):
                val = info.get(candidate)
                if isinstance(val, list):
                    log.debug("exchangeInfo: symbols list found at key %r (%d items)",
                              candidate, len(val))
                    return val

        log.error(
            "exchangeInfo: cannot locate a symbols list. "
            "Top-level keys: %s — run debug_exchange_info.py to inspect the raw response.",
            list(info.keys()) if isinstance(info, dict) else type(info).__name__,
        )
        return []

    def get_all_usdt_spot_symbols(self) -> Set[str]:
        """
        Returns every active USDT spot pair currently listed on MEXC.

        Adaptive filter:
          • quoteAsset == "USDT"        — quote-currency guard
          • status in _ACTIVE_STATUSES  — accepts "ENABLED", "TRADING", 1, True
                                          (run debug_exchange_info.py to see which
                                          value your MEXC instance actually uses)

        isSpotTradingAllowed is NOT checked — MEXC omits it on many valid pairs.
        """
        info = self.get_exchange_info()
        symbols_list = self._extract_symbols_list(info)

        if not symbols_list:
            return set()

        # ── Log status distribution so mismatches are immediately visible ──
        status_dist: Dict[Any, int] = {}
        for s in symbols_list:
            if isinstance(s, dict):
                sv = s.get("status", "<missing>")
                status_dist[sv] = status_dist.get(sv, 0) + 1
        log.info("exchangeInfo status distribution: %s", status_dist)

        # ── Build the set ───────────────────────────────────────────────────
        result: Set[str] = set()
        for s in symbols_list:
            if not isinstance(s, dict):
                continue
            sym = s.get("symbol")
            if not sym:
                continue
            # quoteAsset key: standard is "quoteAsset"; some MEXC docs show "quoteCurrency"
            quote = s.get("quoteAsset") or s.get("quoteCurrency", "")
            status = s.get("status")
            if quote == "USDT" and status in self._ACTIVE_STATUSES:
                result.add(sym)

        log.info("get_all_usdt_spot_symbols: %d active USDT pairs found", len(result))
        return result

    def debug_exchange_info_sample(self, symbol: str = "BTCUSDT") -> Dict:
        """
        Returns the raw exchangeInfo record for one symbol.
        Searches across all possible list locations in the response.
        """
        info = self.get_exchange_info(symbol)
        symbols_list = self._extract_symbols_list(info)
        for s in symbols_list:
            if isinstance(s, dict) and s.get("symbol") == symbol:
                return s
        return {}

    def get_24h_tickers(self) -> List[Dict]:
        """
        Returns rolling 24-hour statistics for every symbol.
        Relevant keys: symbol, lastPrice, volume (base), quoteVolume (USDT),
                       priceChangePercent.
        """
        return self._get("/api/v3/ticker/24hr")

    # ------------------------------------------------------------------ #
    #  Symbol metadata helpers                                             #
    # ------------------------------------------------------------------ #

    def get_symbol_info(self, symbol: str) -> Dict:
        """
        Returns a normalised dict with precision and filter values for a symbol.
        Falls back to safe defaults if the exchange info call fails.
        """
        try:
            info = self.get_exchange_info(symbol)
            for sym in info.get("symbols", []):
                if sym["symbol"] == symbol:
                    result = {
                        "base_precision": sym.get("baseAssetPrecision", 8),
                        "quote_precision": sym.get("quoteAssetPrecision", 8),
                        "min_qty": 0.0,
                        "qty_step": 0.0,
                        "min_notional": 5.0,
                        "tick_size": 0.0,
                    }
                    for f in sym.get("filters", []):
                        ft = f.get("filterType", "")
                        if ft == "LOT_SIZE":
                            result["min_qty"] = float(f.get("minQty", 0))
                            result["qty_step"] = float(f.get("stepSize", 0))
                        elif ft == "MIN_NOTIONAL":
                            result["min_notional"] = float(f.get("minNotional", 5))
                        elif ft == "PRICE_FILTER":
                            result["tick_size"] = float(f.get("tickSize", 0))
                    return result
        except Exception:
            pass
        return {
            "base_precision": 6,
            "quote_precision": 2,
            "min_qty": 0.0,
            "qty_step": 0.0,
            "min_notional": 5.0,
            "tick_size": 0.0,
        }
