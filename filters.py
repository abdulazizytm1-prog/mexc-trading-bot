"""
Protection filters — gate-keeper checks that run before signal generation.

Each function is side-effect-free and returns a plain dict.
All callers should treat these as fast pre-flight checks — no trading
logic lives here, only binary allow/block decisions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

log = logging.getLogger(__name__)

# ── Correlation groups (max 1 open per group) ─────────────────────────────
_CORR_GROUP1 = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"}
_CORR_GROUP2 = {"XRPUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT"}
# Everything not in group 1 or group 2 is group 3 — no intra-group limit.

_ATR_PERIOD = 14


def _calc_atr(candles: List) -> float:
    """14-period ATR from raw MEXC kline list [[open_time,o,h,l,c,v,...], ...]."""
    if len(candles) < _ATR_PERIOD + 1:
        return 0.0
    true_ranges = []
    for i in range(1, len(candles)):
        h      = float(candles[i][2])
        l      = float(candles[i][3])
        prev_c = float(candles[i - 1][4])
        true_ranges.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(true_ranges[-_ATR_PERIOD:]) / _ATR_PERIOD


# ── 1. ATR volatility filter ──────────────────────────────────────────────

def check_atr_filter(candles: List, current_price: float) -> Dict[str, Any]:
    """
    Rejects symbols that are too choppy (ATR% > 3.0) or too dead (ATR% < 0.3).
    Ideal range: 0.5 – 2.0 %.

    Returns:
        tradeable : bool
        atr_pct   : float  (ATR as % of current_price)
        reason    : str
    """
    if current_price <= 0:
        return {"tradeable": False, "atr_pct": 0.0, "reason": "invalid price"}

    atr = _calc_atr(candles)
    if atr <= 0:
        return {"tradeable": False, "atr_pct": 0.0, "reason": "insufficient candles for ATR"}

    atr_pct = atr / current_price * 100.0

    if atr_pct > 3.0:
        return {
            "tradeable": False,
            "atr_pct": round(atr_pct, 3),
            "reason": f"ATR too high ({atr_pct:.2f}%) — choppy market",
        }
    if atr_pct < 0.3:
        return {
            "tradeable": False,
            "atr_pct": round(atr_pct, 3),
            "reason": f"ATR too low ({atr_pct:.2f}%) — dead market",
        }

    return {
        "tradeable": True,
        "atr_pct": round(atr_pct, 3),
        "reason": f"ATR ok ({atr_pct:.2f}%)",
    }


# ── 2. Fake BOS detection ─────────────────────────────────────────────────

def check_fake_bos(candles: List, swing_high: float) -> Dict[str, Any]:
    """
    Detects false break-of-structure signals.

    A BOS is FAKE if ANY of:
      • Close did not break swing_high by at least 0.2 %
      • Price closed back below swing_high within the 2 preceding candles
      • Break candle volume < 14-period average volume

    Returns:
        real_bos   : bool
        confidence : float  (0.0 – 1.0)
        reason     : str
    """
    if not candles or swing_high <= 0:
        return {"real_bos": False, "confidence": 0.0, "reason": "no candles or invalid swing_high"}

    last  = candles[-1]
    close = float(last[4])
    vol   = float(last[5])

    # Rule 1 — minimum break magnitude
    break_pct = (close - swing_high) / swing_high
    if break_pct < 0.002:
        return {
            "real_bos": False,
            "confidence": max(0.0, round(break_pct / 0.002, 2)),
            "reason": f"break too small ({break_pct * 100:.3f}% < 0.20%)",
        }

    # Rule 2 — no immediate reversal within 2 candles
    lookback = candles[-3:-1] if len(candles) >= 3 else candles[:-1]
    for prev in lookback:
        if float(prev[4]) < swing_high:
            return {
                "real_bos": False,
                "confidence": 0.2,
                "reason": "price broke back below swing high within 2 candles",
            }

    # Rule 3 — volume must exceed the 14-candle average
    vol_window = candles[-15:-1] if len(candles) >= 15 else candles[:-1]
    avg_vol = (sum(float(c[5]) for c in vol_window) / len(vol_window)) if vol_window else 0.0
    if avg_vol > 0 and vol < avg_vol:
        return {
            "real_bos": False,
            "confidence": round(min(0.79, vol / avg_vol), 2),
            "reason": f"low volume on break ({vol:.0f} < avg {avg_vol:.0f})",
        }

    vol_ratio   = (vol / avg_vol) if avg_vol > 0 else 1.0
    confidence  = min(1.0, 0.5 + (break_pct / 0.004) * 0.3 + min(vol_ratio - 1.0, 1.0) * 0.2)
    return {
        "real_bos": True,
        "confidence": round(confidence, 2),
        "reason": f"confirmed BOS +{break_pct * 100:.2f}%, vol {vol_ratio:.1f}×avg",
    }


# ── 3. Correlation guard ──────────────────────────────────────────────────

def check_correlation_guard(symbol: str, open_positions: Dict[str, Any]) -> Dict[str, Any]:
    """
    Prevents opening multiple correlated positions simultaneously.

    Groups:
      Group 1 : BTC, ETH, SOL, BNB         — max 1 open at a time
      Group 2 : XRP, ADA, AVAX, DOT, MATIC — max 1 open at a time
      Group 3 : everything else             — no intra-group limit

    Returns:
        allowed : bool
        reason  : str
    """
    def _group(sym: str) -> int:
        if sym in _CORR_GROUP1:
            return 1
        if sym in _CORR_GROUP2:
            return 2
        return 3

    new_group = _group(symbol)
    if new_group == 3:
        return {"allowed": True, "reason": "group 3 — no correlation limit"}

    for existing_sym in open_positions:
        if existing_sym == symbol:
            continue
        if _group(existing_sym) == new_group:
            return {
                "allowed": False,
                "reason": (
                    f"correlation limit: {existing_sym} already open in group {new_group}"
                ),
            }

    return {"allowed": True, "reason": f"group {new_group} — no conflict"}


# ── 4. Order book liquidity check ─────────────────────────────────────────

def check_order_book(symbol: str, entry_price: float, mexc_api: Any) -> Dict[str, Any]:
    """
    Verifies the order book is deep enough to enter cleanly.

    Rejects if:
      • Spread (best_ask − best_bid) / best_bid > 0.15 %
      • Total bid depth within 0.5 % of entry_price < $10,000

    On any API failure returns liquid_enough=True (fail-open — the trade
    is already gated by all other filters; don't add fragility here).

    Returns:
        liquid_enough : bool
        spread_pct    : float
        reason        : str
    """
    try:
        book = mexc_api.get_order_book(symbol, limit=20)
    except Exception as exc:
        log.warning("[%s] Order book fetch failed (%s) — defaulting to allow", symbol, exc)
        return {
            "liquid_enough": True,
            "spread_pct": 0.0,
            "reason": "order book unavailable — fail-open",
        }

    bids: List = book.get("bids", [])
    asks: List = book.get("asks", [])

    if not bids or not asks:
        return {"liquid_enough": False, "spread_pct": 0.0, "reason": "empty order book"}

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])

    if best_bid <= 0:
        return {"liquid_enough": False, "spread_pct": 0.0, "reason": "invalid bid price"}

    spread_pct = (best_ask - best_bid) / best_bid * 100.0

    if spread_pct > 0.15:
        return {
            "liquid_enough": False,
            "spread_pct": round(spread_pct, 4),
            "reason": f"spread too wide ({spread_pct:.3f}% > 0.15%)",
        }

    price_floor   = entry_price * 0.995
    bid_depth_usd = sum(
        float(b[0]) * float(b[1])
        for b in bids
        if float(b[0]) >= price_floor
    )

    if bid_depth_usd < 10_000:
        return {
            "liquid_enough": False,
            "spread_pct": round(spread_pct, 4),
            "reason": f"bid depth too thin (${bid_depth_usd:,.0f} < $10,000)",
        }

    return {
        "liquid_enough": True,
        "spread_pct": round(spread_pct, 4),
        "reason": f"liquid — spread={spread_pct:.3f}%, depth=${bid_depth_usd:,.0f}",
    }


# ── 5. Global market context gate ─────────────────────────────────────────

def check_global_market(market_context: Any, trade_type: str = "") -> Dict[str, Any]:
    """
    Hard macro gates based on the MarketContext dataclass from market_context.py.

    Blocks ALL new entries if:
      • market_change_pct < -3.0 %  — global crash
      • btc_dominance > threshold   — altcoin contagion risk
          swing      : threshold = 60 %
          daytrading : threshold = 65 %
          (default)  : threshold = 58 %
      • fear_greed > 75             — extreme greed (if field present)
      • btc_bias == "BEARISH"       — BTC structure bearish (if field present)

    Args:
        market_context : MarketContext or None
        trade_type     : "swing" | "daytrading" | "" (default conservative)

    Returns:
        tradeable : bool
        reason    : str
    """
    if market_context is None:
        return {"tradeable": True, "reason": "no market context available — fail-open"}

    change_pct = getattr(market_context, "market_change_pct", 0.0)
    if change_pct < -3.0:
        return {
            "tradeable": False,
            "reason": f"global market down {change_pct:.1f}% — no new entries",
        }

    btc_dom = getattr(market_context, "btc_dominance", 0.0)
    if trade_type == "swing":
        btc_dom_limit = 60.0
    elif trade_type == "daytrading":
        btc_dom_limit = 65.0
    else:
        btc_dom_limit = 58.0

    if btc_dom > btc_dom_limit:
        return {
            "tradeable": False,
            "reason": (
                f"BTC dominance {btc_dom:.1f}% > {btc_dom_limit:.0f}%"
                f" ({trade_type or 'default'}) — altcoin risk too high"
            ),
        }

    fear_greed = getattr(market_context, "fear_greed", None)
    if fear_greed is not None and fear_greed > 75:
        return {
            "tradeable": False,
            "reason": f"extreme greed index {fear_greed} — no new entries",
        }

    btc_bias = getattr(market_context, "btc_bias", None)
    if btc_bias is not None and str(btc_bias).upper() == "BEARISH":
        return {
            "tradeable": False,
            "reason": "BTC structure bearish — no long entries",
        }

    return {"tradeable": True, "reason": "global market conditions ok"}
