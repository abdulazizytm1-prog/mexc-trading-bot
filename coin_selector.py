"""
Dynamic coin selection — Coinranking Professional API + MEXC cross-reference.

Refresh pipeline (every COIN_SELECTOR_REFRESH_HOURS):
  1.  Fetch 200 coins from Coinranking ordered by 24h USD volume
  2.  Drop obvious haram/junk coins via symbol + name quick-filter
  3.  Drop coins flagged isWrappedTrustless or lowVolume by Coinranking
  4.  Apply fast numeric gates: mcap ≥ $500M, volume ≥ $50M, |change| ≤ 20%
  5.  Cross-reference with MEXC active USDT spot pairs
  6.  Fetch /coin/{uuid} detail for top 40 candidates (tags, supply, exchange count)
  7.  Fetch /coin/{uuid}/history (7d) for top 40 — weekly trend check
  8.  Fetch /exchanges once — build reputable-exchange reference set
  9.  Apply full halal filter (tag-based: stablecoin, wrapped, meme, lending, …)
  10. Score each candidate 0–10 (see _score_coin)
  11. Keep only score ≥ MIN_COIN_SCORE; sort by score desc, then MEXC volume desc
  12. Cap at MAX_SELECTED_PAIRS; supplement with FALLBACK_PAIRS if too few pass
  13. Save result to active_pairs.json

10-point scoring
----------------
  +2  market cap > $1B        (large cap, lower manipulation risk)
  +1  market cap $500M–$1B    (mid cap)
  +2  24h volume > $100M      (high liquidity)
  +1  24h volume $50M–$100M   (medium liquidity)
  +1  listed on 10+ exchanges (broad distribution, harder to manipulate)
  +1  |change_24h| < 15%      (not in active pump/dump)
  +1  circulating ≥ 90% of total supply (no large unlock overhang)
  +1  tags include L1/L2/infrastructure narrative
  +1  Coinranking tier = 1    (platform's own quality gate)
  +1  sparkline swing < 20%   (intra-day price consistency)
  ──
  10  maximum
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import config
from coin_ranker import CoinRankingClient

log = logging.getLogger(__name__)

_ACTIVE_PAIRS_PATH = Path(__file__).parent / "active_pairs.json"

# ─────────────────────────────────────────────────────────────────────────────
#  Halal filter constants
# ─────────────────────────────────────────────────────────────────────────────

# Tags returned by Coinranking that make a coin ineligible.
HARAM_TAGS: Set[str] = {
    "stablecoin",   # USDT, USDC, DAI, FRAX, etc. — no price movement
    "wrapped",      # WBTC, WETH, etc. — synthetic mirrors, not real assets
    "meme",         # pure speculation with no utility (user's request)
    # Note: "defi" is NOT excluded wholesale — many defi infra tokens are halal.
    # Lending-specific tokens are caught by HARAM_SYMBOLS below.
}

# Tags that POSITIVELY contribute to narrative score.
NARRATIVE_TAGS: Set[str] = {
    "layer-1", "layer-2", "web3",
    "infrastructure",   # not in official list but included defensively
    "dao",              # governance tokens with real utility
    "nft",              # NFT infrastructure (marketplaces, tooling)
    "dex",              # decentralised exchange tokens
    "exchange",         # exchange utility tokens (where non-interest-bearing)
    "privacy",          # privacy coins / infrastructure
    "metaverse",        # metaverse infrastructure
    "gaming",           # blockchain gaming platforms
}

# Exact ticker symbol block-list (uppercased before comparison).
HARAM_SYMBOLS: Set[str] = {
    # Gambling / betting / lottery
    "BET", "DICE", "WINK", "WIN", "ROLL", "LOTTO", "JACKPOT",
    "LUCKY", "SPIN", "POKER", "BACCARAT", "SLOT", "CASINO",
    "ROULETTE", "SHUFFLE", "CSGOROLL",
    # Adult content
    "XXX", "PORN", "NSFW", "ADULT", "SEXC",
    # Alcohol
    "WINE", "BEER", "BREW", "WHISKY", "VODKA", "RUM", "GIN", "SAKE",
    # Weapons / violence
    "GUN", "RIFLE", "BULLET", "BOMB", "AMMO",
    # Interest / riba — lending & yield protocols (user-specified + known)
    "AAVE", "LEND", "COMP", "MKR", "FRAX", "CRV",
    "DAI", "NEXO", "CEL", "XVS", "CREAM", "ALPACA",
    "EULER", "RDNT", "QI", "STRIKE", "IRON", "SILO",
    "MORPHO", "FLUID", "EXACTLY", "ANGLE", "NOTIONAL",
    "TERM", "MAPLE", "GOLDFINCH", "CLEARPOOL", "TRUEFI",
    # Stablecoins — price-pegged, no SMC signal possible
    "USDT", "USDC", "BUSD", "TUSD", "USDP", "GUSD",
    "LUSD", "SUSD", "EUSD", "FDUSD", "PYUSD", "USDD",
    "USDE", "CEUR", "CUSD",
    # Common wrapped tokens (Coinranking's isWrappedTrustless also catches these)
    "WBTC", "WETH", "WBNB", "WSOL", "WAVAX", "WMATIC", "WFTM",
    "WSTETH", "CBBTC", "CBETH",
}

# Substring matches against lowercased coin name.
HARAM_NAME_KEYWORDS: List[str] = [
    "gambling", "casino", " betting", "lottery", "lotto", "jackpot",
    "slot machine", "poker", "baccarat", "roulette",
    "adult content", "pornograph", "erotic",
    "alcohol", " brewery", "distillery",
    "firearms", "ammunition", "arms dealer",
    "lending protocol", "borrowing protocol",
    "interest bearing", "interest-bearing",
    "fixed-rate lending", "undercollateral", "leveraged yield",
]


def _is_haram_basic(symbol: str, name: str) -> bool:
    """Fast check — runs on the list endpoint data before any detail fetch."""
    if symbol.upper() in HARAM_SYMBOLS:
        return True
    name_lower = name.lower()
    return any(kw in name_lower for kw in HARAM_NAME_KEYWORDS)


def _is_haram_by_tags(tags: List[str]) -> bool:
    """Tag-based check — runs only after coin detail is fetched."""
    tag_set = {t.lower() for t in tags}
    return bool(tag_set & HARAM_TAGS)


# ─────────────────────────────────────────────────────────────────────────────
#  Scoring helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sparkline_swing_pct(sparkline: List) -> float:
    """
    Returns the percentage difference between the min and max price in
    the sparkline array.  None / empty entries are skipped.
    """
    prices = [float(p) for p in sparkline if p is not None]
    if len(prices) < 2:
        return 0.0
    lo, hi = min(prices), max(prices)
    if lo <= 0:
        return 100.0
    return (hi - lo) / lo * 100.0


def _weekly_trend_positive(history: Optional[List[Dict]]) -> bool:
    """
    Returns True if the 7-day closing price is higher than the opening price
    (simple uptrend check based on /coin/{uuid}/history data).
    """
    if not history or len(history) < 2:
        return False
    prices = [float(h["price"]) for h in history if h.get("price") is not None]
    if len(prices) < 2:
        return False
    return prices[-1] > prices[0]   # last > first (newest last in CR response)


@dataclass
class ScoreBreakdown:
    mcap:      int = 0   # 0/1/2
    volume:    int = 0   # 0/1/2
    exchanges: int = 0   # 0/1
    stability: int = 0   # 0/1
    supply:    int = 0   # 0/1
    narrative: int = 0   # 0/1
    tier:      int = 0   # 0/1
    sparkline: int = 0   # 0/1

    @property
    def total(self) -> int:
        return (self.mcap + self.volume + self.exchanges +
                self.stability + self.supply + self.narrative +
                self.tier + self.sparkline)


def _score_coin(
    list_data: Dict,
    detail: Optional[Dict],
    history: Optional[List[Dict]],
) -> ScoreBreakdown:
    """
    Scores a coin 0–10 using list + detail + history data.
    Missing data (detail/history not yet fetched) scores 0 on those axes.
    """
    bd = ScoreBreakdown()

    mcap   = float(list_data.get("marketCap")  or 0)
    volume = float(list_data.get("24hVolume")   or 0)
    change = float(list_data.get("change")      or 0)
    tier   = int(list_data.get("tier")          or 2)
    spark  = list_data.get("sparkline")         or []

    # ── Market cap (max 2) ────────────────────────────────────────────────────
    if mcap > 1_000_000_000:
        bd.mcap = 2
    elif mcap >= 500_000_000:
        bd.mcap = 1

    # ── 24h volume (max 2) ────────────────────────────────────────────────────
    if volume > 100_000_000:
        bd.volume = 2
    elif volume >= 50_000_000:
        bd.volume = 1

    # ── Price stability (max 1) ───────────────────────────────────────────────
    if abs(change) < 15.0:
        bd.stability = 1

    # ── Coinranking tier (max 1) ──────────────────────────────────────────────
    if tier == 1:
        bd.tier = 1

    # ── Sparkline intra-day swing (max 1) ─────────────────────────────────────
    if _sparkline_swing_pct(spark) < 20.0:
        bd.sparkline = 1

    # ── Detail-dependent axes ─────────────────────────────────────────────────
    if detail:
        # Exchange distribution (max 1)
        if int(detail.get("numberOfExchanges") or 0) >= 10:
            bd.exchanges = 1

        # Supply health — circulating ≥ 90% of total (max 1)
        supply = detail.get("supply") or {}
        circulating = float(supply.get("circulating") or 0)
        total        = float(supply.get("total")       or 0)
        if circulating > 0 and total > 0 and (circulating / total) >= 0.90:
            bd.supply = 1

        # Narrative tags — L1 / L2 / infrastructure (max 1)
        tags = {t.lower() for t in (detail.get("tags") or [])}
        if tags & NARRATIVE_TAGS:
            bd.narrative = 1

    return bd


# ─────────────────────────────────────────────────────────────────────────────
#  Scored coin — what CoinSelector stores internally
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ScoredCoin:
    mexc_symbol:  str
    symbol:       str
    name:         str
    uuid:         str
    score:        int
    breakdown:    ScoreBreakdown
    tier:         int
    market_cap:   float
    volume_24h:   float
    change_24h:   float
    price_usd:    float
    n_exchanges:  int
    tags:         List[str] = field(default_factory=list)
    weekly_up:    bool = False   # True if 7d history shows uptrend


# ─────────────────────────────────────────────────────────────────────────────
#  CoinSelector
# ─────────────────────────────────────────────────────────────────────────────

class CoinSelector:
    """
    Maintains a dynamic, halal-filtered, quality-scored list of MEXC USDT
    spot pairs.  Call `get_pairs()` each loop — returns the cached list and
    triggers a refresh only when the 4-hour TTL has elapsed.

    Thread-safety: not thread-safe; designed for a single-threaded trading loop.
    """

    def __init__(self, mexc_api) -> None:
        self._api            = mexc_api
        self._lock           = threading.Lock()
        self._pairs:  List[str]         = []
        self._scores: Dict[str, float]  = {}   # mexc_symbol → 0.0–10.0
        self._coins:  List[ScoredCoin]  = []   # full metadata for dashboard
        self._last_refresh: Optional[datetime] = None

    # ── TTL ───────────────────────────────────────────────────────────────────

    def _is_due(self) -> bool:
        with self._lock:
            if not self._pairs or self._last_refresh is None:
                return True
            return (datetime.now() - self._last_refresh) >= timedelta(
                hours=config.COIN_SELECTOR_REFRESH_HOURS
            )

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_active_pairs(self, coins: List[ScoredCoin]) -> None:
        """Writes the current universe to active_pairs.json."""
        next_refresh = (self._last_refresh or datetime.now()) + timedelta(
            hours=config.COIN_SELECTOR_REFRESH_HOURS
        )
        payload = {
            "refreshed_at":  (self._last_refresh or datetime.now()).isoformat(timespec="seconds"),
            "next_refresh":  next_refresh.isoformat(timespec="seconds"),
            "total_pairs":   len(coins),
            "pairs": [
                {
                    "rank":           i + 1,
                    "mexc_symbol":    c.mexc_symbol,
                    "symbol":         c.symbol,
                    "name":           c.name,
                    "uuid":           c.uuid,
                    "score":          c.score,
                    "score_breakdown": asdict(c.breakdown),
                    "tier":           c.tier,
                    "market_cap_usd": round(c.market_cap),
                    "volume_24h_usd": round(c.volume_24h),
                    "change_24h_pct": round(c.change_24h, 2),
                    "price_usd":      c.price_usd,
                    "n_exchanges":    c.n_exchanges,
                    "tags":           c.tags,
                    "weekly_uptrend": c.weekly_up,
                }
                for i, c in enumerate(coins)
            ],
        }
        try:
            _ACTIVE_PAIRS_PATH.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            log.info("[CoinSelector] Saved %d pairs → %s", len(coins), _ACTIVE_PAIRS_PATH.name)
        except OSError as exc:
            log.warning("[CoinSelector] Could not save active_pairs.json: %s", exc)

    # ── Full refresh ──────────────────────────────────────────────────────────

    def refresh(self) -> List[str]:
        """Runs the 13-step pipeline and returns the updated pair list."""
        t0 = time.monotonic()
        log.info("[CoinSelector] ── Starting refresh ──────────────────────────")

        client = CoinRankingClient()

        # ── Step 1: Fetch 200 coins from Coinranking ──────────────────────────
        raw_coins = client.get_all_coins(
            pages=getattr(config, "COINRANKING_PAGES", 2), limit=100
        )
        if not raw_coins:
            log.warning("[CoinSelector] Coinranking unavailable — keeping existing pairs.")
            return self._pairs or list(config.FALLBACK_PAIRS)
        log.info("[CoinSelector] Step 1: %d coins from Coinranking", len(raw_coins))

        # ── Step 2: Symbol + name haram quick-filter ─────────────────────────
        clean = [
            c for c in raw_coins
            if not _is_haram_basic(c.get("symbol", ""), c.get("name", ""))
        ]
        log.info("[CoinSelector] Step 2: %d → %d after basic haram filter",
                 len(raw_coins), len(clean))

        # ── Step 3: Drop wrapped / low-volume flags ───────────────────────────
        clean = [
            c for c in clean
            if not c.get("isWrappedTrustless") and not c.get("lowVolume")
        ]
        log.info("[CoinSelector] Step 3: %d after dropping wrapped/lowVolume",
                 len(clean))

        # ── Step 4: Numeric gates (mcap, volume, change) ─────────────────────
        min_mcap   = getattr(config, "MIN_MARKET_CAP_USD",     500_000_000)
        min_vol    = getattr(config, "MIN_MEXC_24H_VOLUME_USD", 50_000_000)
        max_change = getattr(config, "MAX_CHANGE_PCT",          20.0)

        eligible = [
            c for c in clean
            if float(c.get("marketCap")  or 0) >= min_mcap
            and float(c.get("24hVolume") or 0) >= min_vol
            and abs(float(c.get("change") or 0)) <= max_change
        ]
        log.info("[CoinSelector] Step 4: %d → %d after mcap/volume/change gates",
                 len(clean), len(eligible))

        # ── Step 5: MEXC cross-reference ──────────────────────────────────────
        try:
            mexc_syms: Set[str] = self._api.get_all_usdt_spot_symbols()
            log.info("[CoinSelector] Step 5: %d active USDT pairs on MEXC", len(mexc_syms))
        except Exception as exc:
            log.error("[CoinSelector] MEXC symbol fetch failed: %s", exc)
            mexc_syms = set()

        on_mexc = [
            c for c in eligible
            if f"{c.get('symbol', '').upper()}USDT" in mexc_syms
        ]
        log.info("[CoinSelector] Step 5: %d → %d on MEXC spot", len(eligible), len(on_mexc))

        if not on_mexc:
            log.warning("[CoinSelector] Zero MEXC pairs — using fallback.")
            return self._pairs or list(config.FALLBACK_PAIRS)

        # ── Step 6: Fetch detail for top 50 candidates ────────────────────────
        # Sort by 24h volume (descending) before selecting detail targets
        on_mexc.sort(key=lambda c: float(c.get("24hVolume") or 0), reverse=True)
        detail_targets = on_mexc[:50]
        uuids = [c["uuid"] for c in detail_targets if c.get("uuid")]

        log.info("[CoinSelector] Step 6: fetching detail for %d coins…", len(uuids))
        detail_map: Dict[str, Dict] = client.get_coin_details_batch(uuids)

        # ── Step 7a: Fetch 7-day history (parallel) ───────────────────────────
        log.info("[CoinSelector] Step 7a: fetching 7d history for %d coins (parallel)…", len(uuids))
        history_map: Dict[str, List[Dict]] = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(client.get_coin_history, uid, "7d"): uid for uid in uuids}
            for fut in as_completed(futs):
                uid = futs[fut]
                try:
                    hist = fut.result()
                    if hist:
                        history_map[uid] = hist
                except Exception as exc:
                    log.debug("[CoinSelector] History fetch failed for %s: %s", uid, exc)
        log.info("[CoinSelector] Step 7a: history fetched for %d coins", len(history_map))

        # ── Step 7b: Fetch markets for DEX ratio check (parallel) ─────────────
        log.info("[CoinSelector] Step 7b: fetching markets for DEX ratio (parallel)…")
        dex_ratio_map: Dict[str, float] = {}  # uuid → dex_ratio 0.0–1.0

        def _fetch_dex_ratio(uid: str) -> tuple:
            mkts = client.get_coin_markets(uid, limit=50)
            if not mkts:
                return uid, 0.0
            dex_vol   = sum(float(m.get("quoteVolume") or 0)
                            for m in mkts if (m.get("exchangeType") or "").lower() == "dex")
            total_vol = sum(float(m.get("quoteVolume") or 0) for m in mkts)
            return uid, (dex_vol / total_vol if total_vol > 0 else 0.0)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_dex_ratio, uid): uid for uid in uuids}
            for fut in as_completed(futs):
                uid = futs[fut]
                try:
                    uid_r, ratio = fut.result()
                    dex_ratio_map[uid_r] = ratio
                except Exception as exc:
                    log.debug("[CoinSelector] DEX ratio fetch failed for %s: %s", uid, exc)
        log.info("[CoinSelector] Step 7b: markets fetched for %d coins", len(dex_ratio_map))

        # Filter out coins where DEX volume > 30% of total (manipulation risk)
        _DEX_RATIO_LIMIT = 0.30
        pre_dex = detail_targets[:]
        detail_targets = [
            c for c in detail_targets
            if dex_ratio_map.get(c.get("uuid", ""), 0.0) <= _DEX_RATIO_LIMIT
        ]
        dex_removed = len(pre_dex) - len(detail_targets)
        uuids = [c["uuid"] for c in detail_targets if c.get("uuid")]
        if dex_removed:
            log.info("[CoinSelector] Step 7b: removed %d coins with DEX ratio > %.0f%%",
                     dex_removed, _DEX_RATIO_LIMIT * 100)

        # ── Step 8: Fetch top-exchange reference list ─────────────────────────
        log.info("[CoinSelector] Step 8: fetching top exchanges…")
        top_exchanges = client.get_top_exchanges(limit=30)
        reputable_exchange_names: Set[str] = set()
        if top_exchanges:
            reputable_exchange_names = {
                ex.get("name", "").lower() for ex in top_exchanges if ex.get("name")
            }
            log.info("[CoinSelector] Step 8: %d reputable exchanges loaded",
                     len(reputable_exchange_names))
        else:
            log.warning("[CoinSelector] Step 8: exchange list unavailable")

        # ── Step 9: Full halal filter (tag-based) ─────────────────────────────
        tag_passed: List[Dict] = []
        tag_removed = 0
        for coin in detail_targets:
            uuid   = coin.get("uuid", "")
            detail = detail_map.get(uuid)
            tags   = detail.get("tags", []) if detail else []
            if _is_haram_by_tags(tags):
                log.debug("[CoinSelector] Halal-tag removed: %s (tags=%s)",
                          coin.get("symbol"), tags)
                tag_removed += 1
                continue
            tag_passed.append(coin)

        log.info("[CoinSelector] Step 9: %d → %d after tag-based halal filter (%d removed)",
                 len(detail_targets), len(tag_passed), tag_removed)

        # ── Step 10: Score ─────────────────────────────────────────────────────
        scored: List[ScoredCoin] = []
        for coin in tag_passed:
            uuid    = coin.get("uuid", "")
            detail  = detail_map.get(uuid)
            history = history_map.get(uuid)
            bd      = _score_coin(coin, detail, history)

            sc = ScoredCoin(
                mexc_symbol  = f"{coin.get('symbol', '').upper()}USDT",
                symbol       = (coin.get("symbol") or "").upper(),
                name         = coin.get("name") or coin.get("symbol", ""),
                uuid         = uuid,
                score        = bd.total,
                breakdown    = bd,
                tier         = int(coin.get("tier") or 2),
                market_cap   = float(coin.get("marketCap")  or 0),
                volume_24h   = float(coin.get("24hVolume")  or 0),
                change_24h   = float(coin.get("change")     or 0),
                price_usd    = float(coin.get("price")      or 0),
                n_exchanges  = int((detail or {}).get("numberOfExchanges") or 0),
                tags         = (detail or {}).get("tags") or [],
                weekly_up    = _weekly_trend_positive(history),
            )
            scored.append(sc)

        # ── Step 11: Apply score floor; sort by score then MEXC volume ─────────
        min_score = getattr(config, "MIN_COIN_SCORE", 7)
        qualified = [s for s in scored if s.score >= min_score]
        below = len(scored) - len(qualified)
        log.info(
            "[CoinSelector] Step 11: %d → %d coins with score ≥ %d (%d below threshold)",
            len(scored), len(qualified), min_score, below,
        )

        # Re-rank by MEXC 24h volume for tiebreaking within same score
        try:
            tickers = self._api.get_24h_tickers()
            mexc_vol: Dict[str, float] = {
                t["symbol"]: float(t.get("quoteVolume", 0))
                for t in tickers if isinstance(t, dict)
            }
        except Exception as exc:
            log.warning("[CoinSelector] MEXC 24h volume fetch failed: %s", exc)
            mexc_vol = {}

        qualified.sort(
            key=lambda s: (s.score, mexc_vol.get(s.mexc_symbol, 0.0)),
            reverse=True,
        )

        # ── Step 12: Cap at MAX; supplement if below MIN ───────────────────────
        max_pairs = getattr(config, "MAX_SELECTED_PAIRS", 20)
        min_pairs = getattr(config, "MIN_SELECTED_PAIRS", 5)
        selected = qualified[:max_pairs]

        if len(selected) < min_pairs:
            log.warning(
                "[CoinSelector] Only %d pairs passed — supplementing with fallbacks",
                len(selected),
            )
            existing = {s.mexc_symbol for s in selected}
            for fb in config.FALLBACK_PAIRS:
                if fb not in existing and fb in mexc_syms:
                    sym = fb.replace("USDT", "")
                    selected.append(ScoredCoin(
                        mexc_symbol=fb, symbol=sym, name=fb, uuid="",
                        score=0, breakdown=ScoreBreakdown(), tier=2,
                        market_cap=0, volume_24h=0, change_24h=0,
                        price_usd=0, n_exchanges=0,
                    ))
                    existing.add(fb)
                if len(selected) >= min_pairs:
                    break

        # ── Step 13: Atomically commit results ────────────────────────────────
        _new_pairs  = [s.mexc_symbol for s in selected]
        _new_scores = {s.mexc_symbol: float(s.score) for s in selected}
        with self._lock:
            self._coins        = selected
            self._pairs        = _new_pairs
            self._scores       = _new_scores
            self._last_refresh = datetime.now()

        self._save_active_pairs(selected)

        elapsed = time.monotonic() - t0
        next_at = self._last_refresh + timedelta(hours=config.COIN_SELECTOR_REFRESH_HOURS)

        log.info("[CoinSelector] ── Refresh complete (%.1fs) ──────────────────", elapsed)
        log.info("[CoinSelector] Active pairs (%d):", len(selected))
        for s in selected:
            log.info(
                "  %s  score=%d/10  tier=%d  vol=$%-12s  mcap=$%s  tags=%s",
                s.mexc_symbol.ljust(12), s.score, s.tier,
                f"{s.volume_24h:,.0f}", f"{s.market_cap:,.0f}",
                ",".join(s.tags[:4]) or "—",
            )
        log.info("[CoinSelector] Next refresh: %s", next_at.strftime("%Y-%m-%d %H:%M"))

        return list(self._pairs)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_pairs(self) -> List[str]:
        """
        Returns the cached pair list, triggering a refresh when stale.
        Never returns an empty list — falls back to FALLBACK_PAIRS if needed.
        """
        if self._is_due():
            try:
                self.refresh()
            except Exception as exc:
                log.error("[CoinSelector] Refresh error: %s", exc)
                with self._lock:
                    if not self._pairs:
                        log.warning("[CoinSelector] No cache — using FALLBACK_PAIRS")
                        self._pairs = list(config.FALLBACK_PAIRS)
        with self._lock:
            return list(self._pairs)

    def get_quality_score(self, mexc_symbol: str) -> float:
        """
        Returns the 0–10 Coinranking quality score for a pair.
        Returns 10.0 for unknown symbols so FALLBACK_PAIRS are never blocked.
        """
        with self._lock:
            return self._scores.get(mexc_symbol, 10.0)

    def get_all_scores(self) -> Dict[str, float]:
        """Full score map for logging or the monitoring dashboard."""
        with self._lock:
            return dict(self._scores)

    def get_coin_metadata(self, mexc_symbol: str) -> Optional[ScoredCoin]:
        """Returns the full ScoredCoin record for dashboard use."""
        with self._lock:
            for c in self._coins:
                if c.mexc_symbol == mexc_symbol:
                    return c
        return None
