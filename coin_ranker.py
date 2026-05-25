"""
Coinranking Professional API client.

Wraps all five endpoints used by the coin selection pipeline:
  /coins                    — paginated list with market data
  /coin/{uuid}              — full detail: tags, supply, exchange count
  /coin/{uuid}/history      — price history for trend analysis
  /coin/{uuid}/exchanges    — exchanges listing a coin
  /exchanges                — top-exchange reference list

Auth: x-access-token header (Professional key from config).
All methods return the parsed data dict/list, or None on failure.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

import config

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_BASE          = "https://api.coinranking.com/v2"
_USD_UUID      = "yhjMzLPhuIDl"   # Coinranking's USD reference currency UUID
_TIMEOUT       = 20               # seconds per request
_RETRY_WAIT    = 65               # seconds to wait after a 429
_PAGE_PAUSE    = 0.4              # courtesy delay between paginated calls
_DETAIL_PAUSE  = 0.3              # courtesy delay between per-coin detail calls


class CoinRankingClient:
    """
    Thin, stateless HTTP client for the Coinranking v2 API.
    Instantiate once per refresh cycle; it creates its own requests.Session.
    """

    def __init__(self, api_key: str = "") -> None:
        key = api_key or getattr(config, "COINRANKING_API_KEY", "")
        self._session = requests.Session()
        self._session.headers.update({
            "Accept":          "application/json",
            "x-access-token":  key,
        })

    # ── Transport ─────────────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[Dict] = None) -> Optional[Any]:
        """GET with one 429-retry. Returns parsed JSON or None on error."""
        url = f"{_BASE}{path}"
        for attempt in range(2):
            try:
                resp = self._session.get(url, params=params or {}, timeout=_TIMEOUT)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", _RETRY_WAIT))
                    log.warning("[CoinRanking] Rate limited — waiting %ds", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                body = resp.json()
                if body.get("status") != "success":
                    log.warning("[CoinRanking] Non-success on %s: %s", path, body.get("status"))
                    return None
                return body.get("data")
            except requests.RequestException as exc:
                log.error("[CoinRanking] %s failed (attempt %d): %s", path, attempt + 1, exc)
                if attempt == 0:
                    time.sleep(2)
        return None

    # ── Endpoint 1: /coins ────────────────────────────────────────────────────

    def get_coins_page(
        self,
        offset: int = 0,
        limit: int = 100,
        order_by: str = "24hVolume",
        tiers: Optional[List[int]] = None,
    ) -> Optional[Dict]:
        """
        GET /coins — one page of coins sorted by the given field.
        Returns the raw 'data' dict (contains 'coins' list and 'stats').
        Tiers default to [1, 2] (tier 3 excluded — low quality).
        """
        params: Dict[str, Any] = {
            "referenceCurrencyUuid": _USD_UUID,
            "timePeriod":            "24h",
            "orderBy":               order_by,
            "orderDirection":        "desc",
            "limit":                 limit,
            "offset":                offset,
            "sparkline":             "true",
        }
        # requests encodes list values as repeated params: tiers[]=1&tiers[]=2
        for t in (tiers or [1, 2]):
            params.setdefault("tiers[]", [])
            if isinstance(params["tiers[]"], list):
                params["tiers[]"].append(t)
            else:
                params["tiers[]"] = [params["tiers[]"], t]
        return self._get("/coins", params)

    def get_all_coins(self, pages: int = 2, limit: int = 100) -> List[Dict]:
        """
        Fetches `pages × limit` coins, pausing _PAGE_PAUSE seconds between
        pages to respect free-tier rate limits.
        Returns a flat list of raw coin dicts.
        """
        all_coins: List[Dict] = []
        for page in range(pages):
            if page > 0:
                time.sleep(_PAGE_PAUSE)
            data = self.get_coins_page(offset=page * limit, limit=limit)
            if not data:
                log.warning("[CoinRanking] Page %d returned nothing — stopping early", page + 1)
                break
            coins = data.get("coins", [])
            all_coins.extend(coins)
            log.info("[CoinRanking] /coins page %d: %d coins (total so far: %d)",
                     page + 1, len(coins), len(all_coins))
        return all_coins

    # ── Endpoint 2: /coin/{uuid} ──────────────────────────────────────────────

    def get_coin_detail(self, uuid: str) -> Optional[Dict]:
        """
        GET /coin/{uuid} — full coin profile.

        Key extra fields vs the list endpoint:
          coin.tags[]              — category tags (halal filter + narrative score)
          coin.supply              — {confirmed, circulating, total, max, supplyAt}
          coin.numberOfMarkets     — total trading pairs
          coin.numberOfExchanges   — distinct exchanges listing this coin
          coin.description         — text description
          coin.links[]             — official website, explorer, github
        """
        data = self._get(f"/coin/{uuid}")
        return data.get("coin") if data else None

    def get_coin_details_batch(self, uuids: List[str]) -> Dict[str, Dict]:
        """
        Fetches detail for each UUID, pausing _DETAIL_PAUSE between calls.
        Returns a dict mapping uuid → detail (missing on failure).
        """
        result: Dict[str, Dict] = {}
        for i, uuid in enumerate(uuids):
            if i > 0:
                time.sleep(_DETAIL_PAUSE)
            detail = self.get_coin_detail(uuid)
            if detail:
                result[uuid] = detail
            else:
                log.debug("[CoinRanking] Detail fetch failed for uuid=%s", uuid)
        log.info("[CoinRanking] Fetched detail for %d / %d coins", len(result), len(uuids))
        return result

    # ── Endpoint 3: /coin/{uuid}/history ─────────────────────────────────────

    def get_coin_history(
        self,
        uuid: str,
        time_period: str = "7d",
    ) -> Optional[List[Dict]]:
        """
        GET /coin/{uuid}/history?timePeriod=7d

        Returns a list of {price, timestamp} dicts covering the requested period.
        Used to calculate weekly trend direction and volatility.
        Valid timePeriod values: 1h 3h 12h 24h 7d 30d 3m 1y 3y 5y.
        """
        data = self._get(f"/coin/{uuid}/history", {"timePeriod": time_period,
                                                    "referenceCurrencyUuid": _USD_UUID})
        if not data:
            return None
        return data.get("history")   # list of {price: str, timestamp: int}

    # ── Endpoint 4: /coin/{uuid}/exchanges ────────────────────────────────────

    def get_coin_exchanges(
        self,
        uuid: str,
        limit: int = 20,
    ) -> Optional[List[Dict]]:
        """
        GET /coin/{uuid}/exchanges?limit=20

        Returns exchanges that list this coin, ordered by 24h volume.
        Each entry: {exchangeUuid, exchangeName, 24hVolume, price, ...}
        Used to verify the coin trades on reputable venues.
        """
        data = self._get(f"/coin/{uuid}/exchanges", {"limit": limit})
        if not data:
            return None
        return data.get("exchanges")

    # ── Endpoint 5: /exchanges ────────────────────────────────────────────────

    def get_top_exchanges(self, limit: int = 30) -> Optional[List[Dict]]:
        """
        GET /exchanges?limit=30 — top exchanges by 24h volume.

        Each entry: {uuid, name, 24hVolume, numberOfMarkets, rank, ...}
        Used to build a reference set of reputable exchange names/UUIDs so the
        coin-exchange check can distinguish tier-1 venues from obscure ones.
        """
        data = self._get("/exchanges", {"limit": limit})
        if not data:
            return None
        return data.get("exchanges")

    # ── Endpoint 6: /stats ────────────────────────────────────────────────────

    def get_stats(self) -> Optional[Dict]:
        """
        GET /stats — global crypto market statistics.

        Returns:
          totalCoins        — number of tracked coins
          totalMarkets      — number of tracked markets
          totalExchanges    — number of tracked exchanges
          totalMarketCap    — total market cap in USD (string)
          total24hVolume    — total 24h volume in USD (string)
          btcDominance      — BTC market cap percentage (float, e.g. 54.7)
          bestCoins[]       — top coins by rank
          newestCoins[]     — recently listed coins
        """
        return self._get("/stats")

    # ── Endpoint 7: /coin/{uuid}/markets ──────────────────────────────────────

    def get_coin_markets(
        self,
        uuid: str,
        limit: int = 50,
    ) -> Optional[List[Dict]]:
        """
        GET /coin/{uuid}/markets?limit=50

        Returns markets (trading pairs) for a coin across exchanges.
        Each entry includes:
          exchangeId, exchangeName, exchangeIconUrl
          price, quoteVolume (USD), baseVolume
          exchangeType — "cex" | "dex"
          spread, depth

        Used to compute DEX volume ratio: if >30% of volume is on DEX
        venues the coin may be susceptible to manipulation and should be skipped.
        """
        data = self._get(
            f"/coin/{uuid}/markets",
            {
                "limit":                 limit,
                "referenceCurrencyUuid": _USD_UUID,
                "orderBy":               "24hVolume",
                "orderDirection":        "desc",
            },
        )
        if not data:
            return None
        return data.get("markets")
