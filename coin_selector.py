"""
Dynamic coin selection with halal filtering.

Flow every COIN_SELECTOR_REFRESH_HOURS:
  1. Fetch up to 500 coins from CoinGecko ordered by 24h USD volume
  2. Drop every coin that matches the haram symbol/name rules below
  3. Keep only coins whose USDT pair is active on MEXC spot
  4. Sort survivors by their MEXC 24h USDT volume
  5. Drop any pair below MIN_MEXC_24H_VOLUME_USD
  6. Return the top MAX_SELECTED_PAIRS; supplement with FALLBACK_PAIRS
     if fewer than MIN_SELECTED_PAIRS qualify
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

import requests as _http

import config

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Haram filter                                                        #
# ------------------------------------------------------------------ #

# Exact CoinGecko symbol matches (uppercased before comparison).
# Covers the three categories the user specified — gambling/lottery,
# adult content / alcohol / weapons, and interest-based (riba) DeFi.
HARAM_SYMBOLS: Set[str] = {
    # --- Gambling / Betting / Lottery ---
    "BET",       # BetFury and others
    "DICE",      # PolyDice / Etheroll
    "WINK",      # WINk gambling platform
    "WIN",       # WINk (alt ticker)
    "ROLL",      # Polyroll / RollBit
    "LOTTO",
    "JACKPOT",
    "LUCKY",
    "SPIN",
    "POKER",
    "BACCARAT",
    "SLOT",
    "CASINO",
    "ROULETTE",
    "SHUFFLE",   # Shuffle.com casino
    "CSGOROLL",

    # --- Adult content ---
    "XXX",
    "PORN",
    "NSFW",
    "ADULT",
    "SEXC",

    # --- Alcohol ---
    "WINE",
    "BEER",
    "BREW",
    "WHISKY",
    "VODKA",
    "RUM",
    "GIN",
    "SAKE",

    # --- Weapons / Violence ---
    "GUN",
    "RIFLE",
    "BULLET",
    "BOMB",
    "AMMO",

    # --- Interest / Riba — lending & yield protocols ---
    # (earning/paying interest is riba; governance tokens that profit
    # from interest income on the protocol are treated the same way)
    "AAVE",      # Aave lending — explicitly mentioned by user
    "LEND",      # Old Aave token
    "COMP",      # Compound Finance — explicitly mentioned
    "MKR",       # MakerDAO stability fee (CDP interest) — explicitly mentioned
    "DAI",       # MakerDAO stablecoin: minted only via CDPs that charge a
                 # stability fee (riba); the token itself is backed by an
                 # interest-generating mechanism — explicitly mentioned by user
    "NEXO",      # Centralised crypto lending with interest
    "CEL",       # Celsius Network
    "XVS",       # Venus Protocol (BSC lending)
    "CREAM",     # Cream Finance
    "ALPACA",    # Alpaca Finance (leveraged yield farming)
    "EULER",     # Euler Finance
    "RDNT",      # Radiant Capital
    "QI",        # BENQI lending (Avalanche)
    "STRIKE",    # Strike (lending)
    "IRON",      # Iron Finance
    "SILO",      # Silo Finance
    "MORPHO",    # Morpho lending
    "FLUID",     # Fluid lending
    "EXACTLY",   # Exactly Finance
    "ANGLE",     # Angle Protocol (interest-bearing stablecoins)
    "NOTIONAL",  # Notional Finance (fixed-rate lending)
    "TERM",      # Term Finance
    "MAPLE",     # Maple Finance (institutional lending)
    "GOLDFINCH", # Goldfinch (lending)
    "CLEARPOOL", # Clearpool
    "TRUEFI",    # TrueFi lending
}

# Substring matches run against coin name (lowercased).
# Use narrow, unambiguous strings to minimise false positives.
HARAM_NAME_KEYWORDS: List[str] = [
    # Gambling
    "gambling", "casino", " betting", "lottery", "lotto", "jackpot",
    "slot machine", "poker room", "baccarat", "roulette",
    # Adult
    "adult content", "pornograph", "erotic", " xxx ",
    # Alcohol
    "alcohol", " brewery", "distillery",
    # Weapons
    "firearms", "ammunition", "arms dealer",
    # Interest / lending
    "lending protocol", "borrowing protocol", "interest bearing",
    "interest-bearing", "fixed-rate lending", "undercollateral",
    "leveraged yield",
]


def _is_haram(symbol: str, name: str) -> bool:
    """Returns True if the coin should be excluded on halal grounds."""
    if symbol.upper() in HARAM_SYMBOLS:
        return True
    name_lower = name.lower()
    return any(kw in name_lower for kw in HARAM_NAME_KEYWORDS)


# ------------------------------------------------------------------ #
#  CoinGecko integration                                               #
# ------------------------------------------------------------------ #

_CG_BASE = "https://api.coingecko.com/api/v3"
_CG_TIMEOUT = 20
_CG_PAGE_PAUSE = 2.5   # seconds between page requests (free-tier rate limit)


def _cg_markets_page(page: int, per_page: int = 250) -> Optional[List[Dict]]:
    url = f"{_CG_BASE}/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "volume_desc",
        "per_page": per_page,
        "page": page,
        "sparkline": "false",
        "price_change_percentage": "24h",
    }
    try:
        resp = _http.get(url, params=params, timeout=_CG_TIMEOUT,
                         headers={"Accept": "application/json"})
        if resp.status_code == 429:
            log.warning("[CoinGecko] Rate limited — waiting 65s before retry")
            time.sleep(65)
            resp = _http.get(url, params=params, timeout=_CG_TIMEOUT,
                             headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.error("[CoinGecko] Page %d failed: %s", page, exc)
        return None


def fetch_coingecko_top(pages: int = config.COINGECKO_PAGES) -> List[Dict]:
    """
    Returns up to pages × 250 coins sorted by 24-hour USD volume (descending).
    Each dict includes: symbol, name, total_volume, price_change_percentage_24h.
    """
    coins: List[Dict] = []
    for page in range(1, pages + 1):
        data = _cg_markets_page(page)
        if not data:
            break
        coins.extend(data)
        if page < pages:
            time.sleep(_CG_PAGE_PAUSE)
    log.info("[CoinGecko] Fetched %d coins across %d page(s)", len(coins), pages)
    return coins


# ------------------------------------------------------------------ #
#  Coin selector                                                       #
# ------------------------------------------------------------------ #

class CoinSelector:
    """
    Maintains a dynamic, halal-filtered list of USDT spot pairs to trade.

    Call `get_pairs()` every loop iteration — it returns the cached list
    and triggers a background-style refresh only when the 4-hour window
    has elapsed (or on first call).  If all external APIs fail during a
    refresh the previous list (or FALLBACK_PAIRS) is kept intact.
    """

    def __init__(self, mexc_api) -> None:
        self._api = mexc_api
        self._pairs: List[str] = []
        self._last_refresh: Optional[datetime] = None

    # ---------------------------------------------------------------- #

    def _is_due(self) -> bool:
        if not self._pairs or self._last_refresh is None:
            return True
        return (datetime.now() - self._last_refresh) >= timedelta(
            hours=config.COIN_SELECTOR_REFRESH_HOURS
        )

    def refresh(self) -> List[str]:
        """Full refresh; returns the newly selected pair list."""
        log.info("[CoinSelector] Starting pair-list refresh…")

        # ── Step 1: CoinGecko top coins by 24h volume ───────────────
        cg_coins = fetch_coingecko_top()
        if not cg_coins:
            log.warning("[CoinSelector] CoinGecko unavailable — keeping existing pairs.")
            return self._pairs or list(config.FALLBACK_PAIRS)

        # ── Step 2: Haram filter ─────────────────────────────────────
        halal = [
            c for c in cg_coins
            if not _is_haram(c.get("symbol", ""), c.get("name", ""))
        ]
        removed = len(cg_coins) - len(halal)
        log.info(
            "[CoinSelector] %d → %d coins after haram filter (%d removed)",
            len(cg_coins), len(halal), removed,
        )

        # ── Step 3: MEXC available USDT spot pairs ───────────────────
        try:
            mexc_symbols: Set[str] = self._api.get_all_usdt_spot_symbols()
            log.info("[CoinSelector] MEXC has %d active USDT spot pairs", len(mexc_symbols))
        except Exception as exc:
            log.error("[CoinSelector] Could not fetch MEXC symbol list: %s", exc)
            mexc_symbols = set()

        # ── Step 4: Cross-reference (CoinGecko order = volume rank) ─
        candidates: List[str] = []
        for coin in halal:
            pair = coin["symbol"].upper() + "USDT"
            if pair in mexc_symbols:
                candidates.append(pair)

        log.info("[CoinSelector] %d pairs available on MEXC after cross-reference", len(candidates))

        # ── Step 5: Re-rank by MEXC 24h USDT volume ─────────────────
        try:
            tickers = self._api.get_24h_tickers()
            vol_map: Dict[str, float] = {
                t["symbol"]: float(t.get("quoteVolume", 0))
                for t in tickers
            }
        except Exception as exc:
            log.warning("[CoinSelector] 24h ticker fetch failed: %s — using CoinGecko order", exc)
            vol_map = {}

        if vol_map:
            candidates.sort(key=lambda s: vol_map.get(s, 0.0), reverse=True)

        # ── Step 6: Volume floor ─────────────────────────────────────
        if vol_map:
            before = len(candidates)
            candidates = [
                s for s in candidates
                if vol_map.get(s, 0.0) >= config.MIN_MEXC_24H_VOLUME_USD
            ]
            log.info(
                "[CoinSelector] Volume floor $%s: %d → %d pairs",
                f"{config.MIN_MEXC_24H_VOLUME_USD:,.0f}", before, len(candidates),
            )

        # ── Step 7: Cap at MAX, supplement if below MIN ──────────────
        selected = candidates[:config.MAX_SELECTED_PAIRS]

        if len(selected) < config.MIN_SELECTED_PAIRS:
            log.warning(
                "[CoinSelector] Only %d pairs qualified — supplementing with fallback list",
                len(selected),
            )
            for fb in config.FALLBACK_PAIRS:
                if fb not in selected:
                    selected.append(fb)
                if len(selected) >= config.MIN_SELECTED_PAIRS:
                    break

        self._pairs = selected
        self._last_refresh = datetime.now()
        next_at = self._last_refresh + timedelta(hours=config.COIN_SELECTOR_REFRESH_HOURS)

        log.info(
            "[CoinSelector] Active pairs (%d): %s",
            len(selected), selected,
        )
        log.info(
            "[CoinSelector] Next refresh at %s",
            next_at.strftime("%Y-%m-%d %H:%M"),
        )
        return list(self._pairs)

    def get_pairs(self) -> List[str]:
        """
        Returns the current pair list.
        Triggers a full refresh if the 4-hour window has elapsed.
        On refresh failure the existing list (or FALLBACK_PAIRS) is returned.
        """
        if self._is_due():
            try:
                self.refresh()
            except Exception as exc:
                log.error("[CoinSelector] Refresh raised an unexpected error: %s", exc)
                if not self._pairs:
                    log.warning("[CoinSelector] No pairs cached — using fallback list")
                    self._pairs = list(config.FALLBACK_PAIRS)
        return list(self._pairs)
