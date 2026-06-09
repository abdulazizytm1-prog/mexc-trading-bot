"""
Global market context — Coinranking /v2/stats poller.

Polls /v2/stats every MARKET_CONTEXT_REFRESH_MIN minutes (default 30).
Saves snapshot to market_context.json.

Trade gates exposed:
  is_safe_to_trade()       — False when global market is down > MARKET_DROP_NO_TRADE_PCT in 24h
  is_altcoin_restricted()  — True when BTC dominance > BTC_DOM_ALTCOIN_RESTRICT
  get_context()            — returns the latest MarketContext snapshot

Thread-safety: polling runs in a background daemon thread; reads are lock-protected.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from coin_ranker import CoinRankingClient

log = logging.getLogger(__name__)

_CONTEXT_PATH = Path(__file__).parent / "market_context.json"

# How often to refresh (minutes). Can be overridden in config.
_REFRESH_MIN: int = getattr(config, "MARKET_CONTEXT_REFRESH_HOURS", 0) * 60 or \
                    getattr(config, "MARKET_CONTEXT_REFRESH_MIN", 30)

# Thresholds — read from config so a single edit propagates everywhere
_BTC_DOM_ALTCOIN_RESTRICT: float = getattr(config, "BTC_DOM_ALTCOIN_RESTRICT", 65.0)
_MARKET_DROP_NO_TRADE:     float = getattr(config, "MARKET_DROP_NO_TRADE_PCT",  -3.0)

# Coinranking UUID for Bitcoin (used for 7-day price history)
_BTC_UUID = "Qwsogvtv82FCd"


def detect_market_regime(
    btc_7d_change: float,
    btc_dominance: float,
    fear_greed:    float,
    avg_atr:       float,
) -> str:
    """
    Classify the current market environment into one of four regimes.

    Rules (checked in priority order):
      BEAR     : btc_7d_change < -5%  OR  fear_greed < 25
      BULL     : btc_7d_change >  5%  AND btc_dominance < 58%
      VOLATILE : avg_atr > 2.5%
      RANGING  : everything else
    """
    if btc_7d_change < -5.0 or fear_greed < 25.0:
        return "BEAR"
    if btc_7d_change > 5.0 and btc_dominance < 58.0:
        return "BULL"
    if avg_atr > 2.5:
        return "VOLATILE"
    return "RANGING"


@dataclass
class MarketContext:
    fetched_at:       str    # ISO timestamp
    total_market_cap: float  # USD
    total_24h_volume: float  # USD
    btc_dominance:    float  # percentage, e.g. 54.7
    total_coins:      int
    total_exchanges:  int
    # Derived gate flags (computed at fetch time)
    altcoin_restricted: bool  # True when btc_dominance > 55%
    # We don't have a reliable global 24h-change from /stats; we approximate
    # using total market cap trend stored across two consecutive snapshots.
    prev_market_cap:   float = 0.0
    market_change_pct: float = 0.0   # positive = up, negative = down
    btc_7d_change:     float = 0.0   # BTC price % change over 7 days
    regime:            str   = "RANGING"  # BULL / BEAR / VOLATILE / RANGING


def _calc_btc_7d_change(client: CoinRankingClient) -> float:
    """Fetch BTC 7-day price history from Coinranking and return % change. Returns 0.0 on failure."""
    try:
        history = client.get_coin_history(_BTC_UUID, "7d")
        if history and len(history) >= 2:
            first = float(history[0].get("price") or 0)
            last  = float(history[-1].get("price") or 0)
            if first > 0:
                return (last - first) / first * 100.0
    except Exception as exc:
        log.debug("[MarketContext] BTC 7d change fetch failed: %s", exc)
    return 0.0


def _load_cached() -> Optional[MarketContext]:
    """Read the last saved context from disk (used on startup)."""
    try:
        if _CONTEXT_PATH.exists():
            raw = json.loads(_CONTEXT_PATH.read_text(encoding="utf-8"))
            return MarketContext(**{k: raw[k] for k in MarketContext.__dataclass_fields__ if k in raw})
    except Exception as exc:
        log.debug("[MarketContext] Could not load cached context: %s", exc)
    return None


class MarketContextPoller:
    """
    Background thread that refreshes global market stats every _REFRESH_MIN minutes.
    Instantiate once and call start().
    """

    def __init__(self) -> None:
        self._lock    = threading.Lock()
        self._context: Optional[MarketContext] = _load_cached()
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="market-ctx")
        self._stop    = threading.Event()

    def start(self) -> None:
        self._thread.start()
        log.info("[MarketContext] Poller started (interval=%d min)", _REFRESH_MIN)

    def stop(self) -> None:
        self._stop.set()

    # ── Background loop ───────────────────────────────────────────────────────

    def _loop(self) -> None:
        # Immediate first fetch, then repeat every _REFRESH_MIN minutes.
        while not self._stop.is_set():
            try:
                self._fetch_and_update()
            except Exception as exc:
                log.error("[MarketContext] Fetch error: %s", exc)
            self._stop.wait(_REFRESH_MIN * 60)

    def _fetch_and_update(self) -> None:
        client = CoinRankingClient()
        stats  = client.get_stats()
        if not stats:
            log.warning("[MarketContext] /stats returned nothing — keeping stale data")
            return

        prev_cap      = 0.0
        prev_btc_7d   = 0.0
        prev_regime   = "RANGING"
        with self._lock:
            if self._context:
                prev_cap    = self._context.total_market_cap
                prev_btc_7d = self._context.btc_7d_change
                prev_regime = self._context.regime

        total_cap  = float(stats.get("totalMarketCap")  or 0)
        total_vol  = float(stats.get("total24hVolume")   or 0)
        btc_dom    = float(stats.get("btcDominance")     or 0)
        n_coins    = int(stats.get("totalCoins")         or 0)
        n_exch     = int(stats.get("totalExchanges")     or 0)

        # Approximate 24h market change from consecutive mcap snapshots.
        market_chg = 0.0
        if prev_cap > 0 and total_cap > 0:
            market_chg = (total_cap - prev_cap) / prev_cap * 100.0

        # BTC 7-day change (fall back to cached value on failure)
        btc_7d = _calc_btc_7d_change(client)
        if btc_7d == 0.0 and prev_btc_7d != 0.0:
            btc_7d = prev_btc_7d  # keep last known value on fetch failure

        # Regime detection (fear_greed/avg_atr not available here — defaulted;
        # claude_trader.py refines with live data each scan cycle)
        regime = detect_market_regime(btc_7d, btc_dom, 50.0, 0.0)

        ctx = MarketContext(
            fetched_at        = datetime.now().isoformat(timespec="seconds"),
            total_market_cap  = total_cap,
            total_24h_volume  = total_vol,
            btc_dominance     = btc_dom,
            total_coins       = n_coins,
            total_exchanges   = n_exch,
            altcoin_restricted= btc_dom > _BTC_DOM_ALTCOIN_RESTRICT,
            prev_market_cap   = prev_cap,
            market_change_pct = market_chg,
            btc_7d_change     = btc_7d,
            regime            = regime,
        )

        with self._lock:
            self._context = ctx

        self._save(ctx)

        if regime != prev_regime:
            log.warning(
                "[MarketContext] Regime changed: %s → %s | BTC 7d=%+.1f%%  dom=%.1f%%",
                prev_regime, regime, btc_7d, btc_dom,
            )

        log.info(
            "[MarketContext] Updated — regime=%s  BTC 7d=%+.1f%%  dom=%.1f%%  "
            "mcap=$%.2fT  vol=$%.2fB  altcoin_restricted=%s  market_chg=%.2f%%",
            regime, btc_7d, btc_dom,
            total_cap / 1e12,
            total_vol / 1e9,
            ctx.altcoin_restricted,
            market_chg,
        )

    def _save(self, ctx: MarketContext) -> None:
        try:
            _CONTEXT_PATH.write_text(
                json.dumps(asdict(ctx), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("[MarketContext] Could not save market_context.json: %s", exc)

    # ── Public gates ──────────────────────────────────────────────────────────

    def get_context(self) -> Optional[MarketContext]:
        with self._lock:
            return self._context

    def is_safe_to_trade(self) -> bool:
        """
        Returns False when the approximate 24h global market change is below
        _MARKET_DROP_NO_TRADE (-3%).  True on first run (no history yet).
        """
        with self._lock:
            ctx = self._context
        if ctx is None or ctx.prev_market_cap == 0:
            return True  # no history yet — allow trading
        return ctx.market_change_pct >= _MARKET_DROP_NO_TRADE

    def is_altcoin_restricted(self) -> bool:
        """
        Returns True when BTC dominance exceeds config.BTC_DOM_ALTCOIN_RESTRICT.
        Caller should reduce the number of altcoin entries when this is True.
        """
        with self._lock:
            ctx = self._context
        if ctx is None:
            return False
        return ctx.altcoin_restricted
