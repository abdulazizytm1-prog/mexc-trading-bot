"""
Real-time price feed — Coinranking WebSocket /rates + /tickers.

Maintains a thread-safe in-memory cache of:
  _prices  : {coinranking_uuid → float}   (from /rates stream)
  _tickers : {coinranking_uuid → TickerData}  (from /tickers stream)

Usage:
    feed = WSPriceFeed(uuid_map)   # uuid_map: {mexc_symbol → cr_uuid}
    feed.start()
    ...
    price = feed.get_price("BTCUSDT")     # returns float or None
    dex_ratio = feed.get_dex_ratio("BTCUSDT")  # 0.0–1.0

WSPriceFeed reconnects automatically on disconnect.
Both connections share one asyncio event loop running in a daemon thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config

log = logging.getLogger(__name__)

_WS_BASE      = "wss://api.coinranking.com/v2/real-time"
_THROTTLE     = "1s"
_RECONNECT_S  = 10   # seconds to wait before reconnect attempt


@dataclass
class TickerData:
    uuid:         str
    price:        float = 0.0
    base_volume:  float = 0.0   # coin volume
    quote_volume: float = 0.0   # USD volume
    dex_volume:   float = 0.0   # volume attributed to DEX markets
    cex_volume:   float = 0.0   # volume attributed to CEX markets
    updated_at:   float = field(default_factory=time.time)

    @property
    def dex_ratio(self) -> float:
        """Fraction of volume on DEX venues (0.0–1.0)."""
        total = self.dex_volume + self.cex_volume
        if total <= 0:
            return 0.0
        return self.dex_volume / total


class WSPriceFeed:
    """
    Subscribes to Coinranking /rates and /tickers WebSocket streams.
    Both run as concurrent coroutines inside one background asyncio event loop.

    Parameters
    ----------
    uuid_map : dict mapping MEXC symbol → Coinranking UUID
               e.g. {"BTCUSDT": "Qwsogvtv82FCd", "ETHUSDT": "razxDUgYGNAdQ"}
    """

    def __init__(self, uuid_map: Dict[str, str]) -> None:
        self._uuid_map    = dict(uuid_map)                    # mexc_sym → cr_uuid
        self._rev_map     = {v: k for k, v in uuid_map.items()}  # cr_uuid → mexc_sym
        self._lock        = threading.Lock()
        self._prices:  Dict[str, float]      = {}   # cr_uuid → price
        self._tickers: Dict[str, TickerData] = {}   # cr_uuid → TickerData
        self._loop:   Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread]          = None
        self._stop    = threading.Event()

        # 15-minute price snapshots for movement trigger detection
        self._snap15:    Dict[str, float] = {}   # cr_uuid → price at snapshot
        self._snap15_ts: float            = 0.0  # Unix timestamp of last snapshot

        # 403 circuit-breaker — shared across both streams (same API key)
        self._403_strikes: int  = 0     # consecutive 403 errors
        self._banned:      bool = False  # True after 3 strikes — caller must use REST

    # ── Public API ────────────────────────────────────────────────────────────

    def update_uuid_map(self, uuid_map: Dict[str, str]) -> None:
        """Hot-swap the UUID map (called after a coin selector refresh)."""
        with self._lock:
            self._uuid_map = dict(uuid_map)
            self._rev_map  = {v: k for k, v in uuid_map.items()}

    def get_price(self, mexc_symbol: str) -> Optional[float]:
        """Returns the latest real-time price for a MEXC symbol, or None."""
        uuid = self._uuid_map.get(mexc_symbol)
        if not uuid:
            return None
        with self._lock:
            return self._prices.get(uuid)

    def get_ticker(self, mexc_symbol: str) -> Optional[TickerData]:
        """Returns the latest TickerData for a MEXC symbol, or None."""
        uuid = self._uuid_map.get(mexc_symbol)
        if not uuid:
            return None
        with self._lock:
            return self._tickers.get(uuid)

    def get_dex_ratio(self, mexc_symbol: str) -> float:
        """Returns the DEX volume ratio (0.0–1.0) for a MEXC symbol."""
        td = self.get_ticker(mexc_symbol)
        return td.dex_ratio if td else 0.0

    def is_dex_spike(self, mexc_symbol: str, threshold: float = 0.30) -> bool:
        """Returns True when DEX volume exceeds `threshold` fraction of total."""
        return self.get_dex_ratio(mexc_symbol) > threshold

    def get_move_pct_15m(self, mexc_symbol: str) -> float:
        """
        Returns the price change % for a MEXC symbol over the last 15 minutes.
        Returns 0.0 if no snapshot is available yet.
        """
        uuid = self._uuid_map.get(mexc_symbol)
        if not uuid:
            return 0.0
        with self._lock:
            current  = self._prices.get(uuid, 0.0)
            baseline = self._snap15.get(uuid, 0.0)
        if not current or not baseline:
            return 0.0
        return (current - baseline) / baseline * 100.0

    def get_triggered_symbols(self, threshold_pct: float = 1.5) -> List[str]:
        """
        Returns MEXC symbols whose price moved more than `threshold_pct` %
        in the last 15 minutes (abs value — catches both pumps and dumps).
        """
        return [
            sym for sym in list(self._uuid_map.keys())
            if abs(self.get_move_pct_15m(sym)) >= threshold_pct
        ]

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_healthy(self) -> bool:
        """False after 3 consecutive HTTP 403s. Caller should switch to REST price data."""
        return not self._banned

    def _maybe_refresh_snapshots(self) -> None:
        """Takes a 15-min price snapshot. Called on every rate tick — cheap."""
        now = time.time()
        if now - self._snap15_ts < 900:   # 15 minutes
            return
        with self._lock:
            self._snap15    = dict(self._prices)
            self._snap15_ts = now
        log.debug("[WSFeed] 15-min price snapshot refreshed (%d entries)", len(self._snap15))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ws-price-feed"
        )
        self._thread.start()
        log.info("[WSFeed] Started (tracking %d symbols)", len(self._uuid_map))

    def stop(self) -> None:
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── Event loop (daemon thread) ────────────────────────────────────────────

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            loop.close()

    async def _main(self) -> None:
        while not self._stop.is_set():
            if self._banned:
                log.warning("[WSFeed] HTTP 403 ban active — WebSocket feed stopped. Bot using REST fallback.")
                return
            uuids = list(self._uuid_map.values())
            if not uuids:
                await asyncio.sleep(5)
                continue
            try:
                await asyncio.gather(
                    self._connect_rates(uuids),
                    self._connect_tickers(uuids),
                )
            except Exception as exc:
                if self._banned:
                    return
                log.error("[WSFeed] Connection error: %s — reconnecting in %ds", exc, _RECONNECT_S)
                await asyncio.sleep(_RECONNECT_S)

    # ── /rates stream ─────────────────────────────────────────────────────────

    async def _connect_rates(self, uuids: list) -> None:
        try:
            import websockets  # lazy import so it's optional at class definition
        except ImportError:
            log.error("[WSFeed] 'websockets' package not installed — run: pip install websockets")
            return

        api_key = getattr(config, "COINRANKING_API_KEY", "")
        url = f"{_WS_BASE}/rates?x-access-token={api_key}"
        subscribe_msg = json.dumps({"currencyUuids": uuids, "throttle": _THROTTLE})
        log.debug("[WSFeed/rates] Connecting to %s/rates", _WS_BASE)

        while not self._stop.is_set():
            if self._banned:
                return
            try:
                async with websockets.connect(url) as ws:
                    self._403_strikes = 0   # successful handshake resets the counter
                    await ws.send(subscribe_msg)
                    log.info("[WSFeed/rates] Connected (%d uuids)", len(uuids))
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        self._handle_rate(raw)
            except Exception as exc:
                if self._stop.is_set():
                    return
                exc_str = str(exc).replace(api_key, "***") if api_key else str(exc)
                if "403" in str(exc) or "forbidden" in str(exc).lower():
                    self._403_strikes += 1
                    if self._403_strikes >= 3:
                        self._banned = True
                        log.warning(
                            "[WSFeed/rates] HTTP 403 received %d times in a row — "
                            "stopping WebSocket retries. Bot will use REST API for prices.",
                            self._403_strikes,
                        )
                        return
                    log.warning("[WSFeed/rates] HTTP 403 (%d/3) — retry in %ds", self._403_strikes, _RECONNECT_S)
                else:
                    self._403_strikes = 0
                    log.warning("[WSFeed/rates] Disconnected: %s — retry in %ds", exc_str, _RECONNECT_S)
                await asyncio.sleep(_RECONNECT_S)

    def _handle_rate(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            uuid  = msg.get("currencyUuid") or msg.get("uuid")
            price = msg.get("price")
            if uuid and price is not None:
                with self._lock:
                    self._prices[uuid] = float(price)
            self._maybe_refresh_snapshots()
        except Exception:
            pass

    # ── /tickers stream ───────────────────────────────────────────────────────

    async def _connect_tickers(self, uuids: list) -> None:
        try:
            import websockets
        except ImportError:
            return  # already logged in _connect_rates

        api_key = getattr(config, "COINRANKING_API_KEY", "")
        url = f"{_WS_BASE}/tickers?x-access-token={api_key}"
        subscribe_msg = json.dumps({"currencyUuids": uuids, "throttle": _THROTTLE})
        log.debug("[WSFeed/tickers] Connecting to %s/tickers", _WS_BASE)

        while not self._stop.is_set():
            if self._banned:
                return
            try:
                async with websockets.connect(url) as ws:
                    self._403_strikes = 0   # successful handshake resets the counter
                    await ws.send(subscribe_msg)
                    log.info("[WSFeed/tickers] Connected (%d uuids)", len(uuids))
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        self._handle_ticker(raw)
            except Exception as exc:
                if self._stop.is_set():
                    return
                exc_str = str(exc).replace(api_key, "***") if api_key else str(exc)
                if "403" in str(exc) or "forbidden" in str(exc).lower():
                    self._403_strikes += 1
                    if self._403_strikes >= 3:
                        self._banned = True
                        log.warning(
                            "[WSFeed/tickers] HTTP 403 received %d times in a row — "
                            "stopping WebSocket retries. Bot will use REST API for prices.",
                            self._403_strikes,
                        )
                        return
                    log.warning("[WSFeed/tickers] HTTP 403 (%d/3) — retry in %ds", self._403_strikes, _RECONNECT_S)
                else:
                    self._403_strikes = 0
                    log.warning("[WSFeed/tickers] Disconnected: %s — retry in %ds", exc_str, _RECONNECT_S)
                await asyncio.sleep(_RECONNECT_S)

    def _handle_ticker(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            uuid  = msg.get("currencyUuid") or msg.get("uuid")
            price = msg.get("close") or msg.get("price")
            if not uuid or price is None:
                return

            exchange_type = (msg.get("exchangeType") or "").lower()
            quote_vol = float(msg.get("quoteVolume") or 0)
            base_vol  = float(msg.get("baseVolume")  or 0)

            with self._lock:
                td = self._tickers.get(uuid) or TickerData(uuid=uuid)
                td.price       = float(price)
                td.base_volume = base_vol
                td.updated_at  = time.time()

                if exchange_type == "dex":
                    td.dex_volume = quote_vol   # set, not accumulate — prevents ratio drift to 1.0
                elif exchange_type == "cex":
                    td.cex_volume = quote_vol

                self._tickers[uuid] = td
                # Keep _prices in sync with ticker updates too
                self._prices[uuid]  = float(price)
        except Exception:
            pass
