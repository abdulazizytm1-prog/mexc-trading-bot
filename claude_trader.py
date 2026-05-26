"""
claude_trader.py — AI-powered trade decision engine.

Every 5 minutes during London (07:00–10:00 UTC) and NY (13:00–16:00 UTC) kill
zones, this engine runs the ICT/SMC strategy, passes qualifying signals (score ≥ 8)
to Claude for a second-opinion review, and only executes when Claude returns
decision=BUY with confidence ≥ 8.

Deployment note
---------------
Run this INSTEAD of main.py on Railway (not simultaneously). main.py is the
automatic mode; claude_trader.py is the AI-supervised mode. Both share
positions.json for persistence and the same RiskManager rules.

Fallback
--------
If the Anthropic API is unavailable, the signal is logged as SKIPPED and not
executed (conservative default). main.py would execute it automatically if you
switch over.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic

import config
import news_filter
import telegram_alerts as tg
from coin_selector import CoinSelector
from filters import check_atr_filter, check_correlation_guard, check_global_market, check_order_book
from market_context import MarketContextPoller
from mexc_api import MEXCAPIError, MEXCSpotAPI
from risk_manager import Position, RiskManager
from strategy import (
    TradeSignal,
    candles_to_df,
    detect_kill_zone,
    detect_market_structure,
    generate_signal,
)
from ws_price_feed import WSPriceFeed


# ------------------------------------------------------------------ #
#  Logging setup                                                       #
# ------------------------------------------------------------------ #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("claude_trader.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# Dedicated decision log — plain text, one line per Claude verdict.
_decision_log = logging.getLogger("claude_decisions")
if not _decision_log.handlers:
    _dfh = logging.FileHandler("claude_decisions.log", encoding="utf-8")
    _dfh.setFormatter(logging.Formatter("%(message)s"))
    _decision_log.addHandler(_dfh)
    _decision_log.propagate = False

_JOURNAL_PATH          = Path(__file__).parent / "trade-journal.json"
_LOOP_INTERVAL         = 300   # 5 minutes between scans
_SMART_EXIT_INTERVAL   = 900   # 15 minutes between smart exit checks
_CLAUDE_MODEL          = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
_MAX_CLAUDE_CANDIDATES = 3     # top-N signals ranked by score and sent to Claude per cycle


# ------------------------------------------------------------------ #
#  Claude system prompt                                                #
# ------------------------------------------------------------------ #

_EXIT_SYSTEM_PROMPT = """
You are a professional ICT/SMC trader managing an open long position.
Decide whether to hold, close, or adjust it.

HOLD         = structure intact, momentum valid, no reason to exit
CLOSE        = structure broken, BTC bearish, position invalidated
MOVE_SL      = raise stop loss to protect profit (include new_sl price)
PARTIAL_CLOSE = close 50% to lock some profit, hold the rest

Respond ONLY in this exact JSON format:
{
  "decision": "HOLD" or "CLOSE" or "MOVE_SL" or "PARTIAL_CLOSE",
  "reason": "brief explanation (1-2 sentences)",
  "new_sl": null or float price (only when decision is MOVE_SL)
}
""".strip()

_SYSTEM_PROMPT = """
You are a professional ICT/SMC institutional trader.
Your job: analyze trade signals and protect capital.

STRICT RULES:
- Only approve trades with 8+/10 confidence
- BTC bearish structure → NO TRADE
- Fear & Greed > 75 → NO TRADE
- Signal score < 8 → NO TRADE
- RR < 1:3 → NO TRADE
- Fake BOS suspected → NO TRADE
- When in doubt → NO TRADE
- Capital protection is ALWAYS priority

Respond ONLY in this JSON format:
{
  "decision": "BUY" or "NO_TRADE",
  "confidence": 0-10,
  "reason": "brief explanation",
  "risk_level": "LOW/MEDIUM/HIGH",
  "invalidation": "what would cancel this trade"
}
""".strip()


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _round_step(value: float, step: float, precision: int) -> float:
    if step > 0:
        value = math.floor(value / step) * step
    return round(value, precision)


def _load_journal() -> list:
    if _JOURNAL_PATH.exists():
        try:
            return json.loads(_JOURNAL_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_journal(entries: list) -> None:
    try:
        _JOURNAL_PATH.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not write trade-journal.json: %s", exc)


# ------------------------------------------------------------------ #
#  ClaudeTrader                                                        #
# ------------------------------------------------------------------ #

class ClaudeTrader:
    """
    End-to-end trading engine that gates every entry through Claude.

    Flow per cycle:
      1. Kill-zone check (London 07-10, NY 13-16 only)
      2. Global market filter (check_global_market)
      3. Risk-manager limits (can_open_position)
      4. Per-symbol pipeline: ATR → correlation → order book →
                              4H structure → strategy signal (score ≥ 8)
      5. Claude review → execute only on BUY + confidence ≥ 8
    """

    def __init__(self) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to your .env file or Railway environment variables."
            )
        self._client     = anthropic.Anthropic(api_key=api_key)
        self._api        = MEXCSpotAPI()
        self._risk_mgr   = RiskManager()
        self._coin_sel   = CoinSelector(self._api)

        # Background services start lazily after initial pair load
        self._market_ctx: Optional[MarketContextPoller] = None
        self._ws_feed:    Optional[WSPriceFeed]          = None

        self._sym_info_cache: Dict[str, dict] = {}
        self._balance: float = 0.0

        # Daily trade outcome counters (reset each calendar day)
        self._daily_wins:   int  = 0
        self._daily_losses: int  = 0
        self._summary_sent_date: Optional[str] = None   # ISO date of last summary

        # Scan diagnostic state — populated each cycle, read by /status
        self._scan_stats:          Dict[str, int] = {}
        self._last_scan_summary:   str             = ""
        self._last_scan_top_blocker: str           = ""

        # Smart exit timer — checked every 15 min regardless of entry gate state
        self._smart_exit_timer: float = 0.0

        # Telegram command handler — starts after MEXC connection is confirmed
        self._cmd_handler = tg.TelegramCommandHandler(self)

    # ---------------------------------------------------------------- #
    #  Internal helpers                                                 #
    # ---------------------------------------------------------------- #

    def _determine_trade_type(self) -> str:
        """Return 'swing' for 4H/Daily timeframes, 'daytrading' for 1H/15M."""
        tf = getattr(config, "PRIMARY_TIMEFRAME", "60m").lower()
        if tf in ("4h", "1d"):
            return "swing"
        return "daytrading"

    def _evaluate_coin_signal(self, symbol: str, trade_type: str = "") -> Optional[TradeSignal]:
        """
        Run all pre-Claude filters for one symbol and return a qualifying
        TradeSignal, or None if any filter rejects it.

        Filter order:
          DEX spike → ATR → correlation → position limits → order book →
          4H structure (BEARISH always blocked; NEUTRAL allowed for daytrading) →
          strategy signal (score ≥ 8, strength ≥ 0.65) →
          active FVG or OB zone required → price within 1% of zone.

        Emits [Diag] log lines at each checkpoint and updates self._scan_stats
        for the end-of-cycle summary.  No filter thresholds are changed here.
        """
        st = self._scan_stats   # reference to the cycle's shared counter dict

        if self._ws_feed and self._ws_feed.is_dex_spike(symbol):
            pct = self._ws_feed.get_dex_ratio(symbol) * 100 if self._ws_feed else 0
            log.info("[Diag] %s | DEX spike: %.0f%% ✗", symbol, pct)
            st["dex_spike"] = st.get("dex_spike", 0) + 1
            return None

        try:
            klines = self._api.get_klines(
                symbol, config.PRIMARY_TIMEFRAME, config.CANDLE_LIMIT
            )
            if not klines:
                return None

            ws_price      = self._ws_feed.get_price(symbol) if self._ws_feed else None
            current_price = ws_price or float(klines[-1][4])

            # ── ATR filter ────────────────────────────────────────────────
            atr_check = check_atr_filter(klines, current_price)
            if not atr_check["tradeable"]:
                log.info(
                    "[Diag] %s | ATR: %.2f%% ✗ (%s)",
                    symbol, atr_check.get("atr_pct", 0.0), atr_check["reason"],
                )
                st["atr"] = st.get("atr", 0) + 1
                return None
            log.info("[Diag] %s | ATR: %.2f%% ✓", symbol, atr_check.get("atr_pct", 0.0))

            # ── Correlation guard ──────────────────────────────────────────
            corr_check = check_correlation_guard(symbol, self._risk_mgr.all_positions())
            if not corr_check["allowed"]:
                log.info(
                    "[Diag] %s | Correlation: blocked (%s) ✗",
                    symbol, corr_check["reason"],
                )
                st["correlation"] = st.get("correlation", 0) + 1
                return None
            log.info("[Diag] %s | Correlation: allowed ✓", symbol)

            # ── Per-symbol position limit ──────────────────────────────────
            if not self._risk_mgr.can_open_position(symbol):
                log.info("[Diag] %s | Position limit: already open ✗", symbol)
                st["position_limit"] = st.get("position_limit", 0) + 1
                return None

            # ── Order book liquidity ───────────────────────────────────────
            ob_check = check_order_book(symbol, current_price, self._api)
            if not ob_check["liquid_enough"]:
                log.info(
                    "[Diag] %s | OrderBook: spread=%.3f%% ✗ (%s)",
                    symbol, ob_check.get("spread_pct", 0.0), ob_check["reason"],
                )
                st["order_book"] = st.get("order_book", 0) + 1
                return None
            log.info(
                "[Diag] %s | OrderBook: spread=%.3f%% ✓",
                symbol, ob_check.get("spread_pct", 0.0),
            )

            # ── 4H market structure — must be BULLISH ─────────────────────
            htf_df   = None
            htf_bias = "N/A"
            try:
                htf_klines = self._api.get_klines(
                    symbol,
                    getattr(config, "HTF_TIMEFRAME", "4h"),
                    getattr(config, "HTF_CANDLE_LIMIT", 50),
                )
                htf_df = candles_to_df(htf_klines) if htf_klines else None
            except Exception:
                pass

            if htf_df is not None and not htf_df.empty:
                structure = detect_market_structure(htf_df)
                htf_bias  = structure.get("bias", "UNKNOWN")
                # Daytrading: only BEARISH blocks; NEUTRAL is acceptable
                # Swing/default: must be BULLISH
                if trade_type == "daytrading":
                    if htf_bias == "BEARISH":
                        log.info("[Diag] %s | 4H Structure: BEARISH ✗", symbol)
                        st["structure"] = st.get("structure", 0) + 1
                        return None
                    log.info("[Diag] %s | 4H Structure: %s ✓ (daytrading)", symbol, htf_bias)
                else:
                    if htf_bias != "BULLISH":
                        log.info("[Diag] %s | 4H Structure: %s ✗", symbol, htf_bias)
                        st["structure"] = st.get("structure", 0) + 1
                        return None
                    log.info("[Diag] %s | 4H Structure: BULLISH ✓", symbol)
            else:
                log.info("[Diag] %s | 4H Structure: N/A (no HTF data) ~", symbol)

            # ── Strategy signal ────────────────────────────────────────────
            df = candles_to_df(klines)
            if df.empty:
                return None

            signal = generate_signal(symbol, df, htf_df=htf_df, trade_type=trade_type)

            if signal is None:
                log.info("[Diag] %s | Signal: None (no setup found) ✗", symbol)
                st["no_signal"] = st.get("no_signal", 0) + 1
                return None

            if signal.score < 8:
                log.info(
                    "[Diag] %s | Signal: score=%d/10 ✗ (below 8)",
                    symbol, signal.score,
                )
                if not signal.liquidity_sweep:
                    log.info("[Diag] %s | Signal: No liquidity sweep ✗", symbol)
                if not signal.displacement:
                    log.info("[Diag] %s | Signal: No displacement candle ✗", symbol)
                if not getattr(signal, "discount_zone", False):
                    log.info("[Diag] %s | Signal: Not in discount zone ✗", symbol)
                if not (signal.fvg_present or signal.ob_present):
                    log.info("[Diag] %s | Signal: NO FVG/OB near price ✗", symbol)
                st["low_score"] = st.get("low_score", 0) + 1
                return None

            if signal.strength < 0.65:
                log.info(
                    "[Diag] %s | Signal: score=%d/10 strength=%.2f ✗ (weak signal)",
                    symbol, signal.score, signal.strength,
                )
                st["low_strength"] = st.get("low_strength", 0) + 1
                return None

            # ── Active FVG or OB zone required ────────────────────────────
            if not (signal.fvg_present or signal.ob_present):
                log.info(
                    "[Diag] %s | Signal: score=%d/10 NO FVG/OB near price ✗",
                    symbol, signal.score,
                )
                st["no_zone"] = st.get("no_zone", 0) + 1
                return None

            # ── Price within 1% of entry zone ─────────────────────────────
            if current_price > 0:
                proximity_pct = abs(current_price - signal.entry_price) / signal.entry_price
                if proximity_pct > 0.01:
                    log.info(
                        "[Diag] %s | Signal: score=%d/10 price %.2f%% from zone ✗ (>1%%)",
                        symbol, signal.score, proximity_pct * 100,
                    )
                    st["price_far"] = st.get("price_far", 0) + 1
                    return None

            log.info(
                "[Diag] %s | Signal: score=%d/10 zone=%s strength=%.2f ✓ → qualified",
                symbol, signal.score, signal.zone_type, signal.strength,
            )
            st["approved"] = st.get("approved", 0) + 1
            return signal

        except MEXCAPIError as exc:
            log.error("[%s] API error during signal evaluation: %s", symbol, exc)
            return None
        except Exception as exc:
            log.exception("[%s] Unexpected error during signal evaluation: %s", symbol, exc)
            return None

    def _ensure_sym_info(self, symbol: str) -> dict:
        if symbol not in self._sym_info_cache:
            try:
                self._sym_info_cache[symbol] = self._api.get_symbol_info(symbol)
            except Exception:
                self._sym_info_cache[symbol] = {
                    "base_precision": 6, "quote_precision": 2,
                    "min_qty": 0.0, "qty_step": 0.0,
                    "min_notional": 5.0, "tick_size": 0.0,
                }
        return self._sym_info_cache[symbol]

    # ---------------------------------------------------------------- #
    #  Smart exit — helpers                                            #
    # ---------------------------------------------------------------- #

    @staticmethod
    def _calc_rsi(closes: List[float], period: int = 14) -> List[float]:
        """Wilder's RSI. Returns values for indices [period..len(closes)-1]."""
        if len(closes) < period + 2:
            return []
        gains  = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
        losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
        avg_g  = sum(gains[:period])  / period
        avg_l  = sum(losses[:period]) / period
        result: List[float] = []
        for i in range(period, len(gains)):
            avg_g = (avg_g * (period - 1) + gains[i])  / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
            result.append(100.0 if avg_l == 0.0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l))
        return result

    def _btc_market_check(self) -> Dict[str, Any]:
        """
        Fetch BTCUSDT 1H klines and return:
          bias, pct_change_1h, volume_ratio (last candle vs 20-bar avg).
        Returns safe defaults on any error.
        """
        default: Dict[str, Any] = {
            "bias": "UNKNOWN", "pct_change_1h": 0.0, "volume_ratio": 1.0,
        }
        try:
            klines = self._api.get_klines("BTCUSDT", "60m", 50)
            if not klines or len(klines) < 5:
                return default
            df      = candles_to_df(klines)
            bias    = detect_market_structure(df).get("bias", "UNKNOWN")
            closes  = df["close"].astype(float).values
            pct_1h  = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] > 0 else 0.0
            vols    = df["volume"].astype(float).values
            avg_vol = float(vols[-21:-1].mean()) if len(vols) > 20 else float(vols.mean())
            vol_ratio = float(vols[-1]) / avg_vol if avg_vol > 0 else 1.0
            return {"bias": bias, "pct_change_1h": pct_1h, "volume_ratio": vol_ratio}
        except Exception as exc:
            log.debug("[SmartExit] BTC check error: %s", exc)
            return default

    @staticmethod
    def _rsi_divergence(df: Any, lookback: int = 20) -> bool:
        """
        Returns True if bearish divergence: last N candles have price sloping UP
        while RSI(14) slopes DOWN by more than 3 points.
        """
        try:
            closes = list(df["close"].astype(float).values[-lookback:])
            if len(closes) < 18:
                return False
            rsi = ClaudeTrader._calc_rsi(closes, 14)
            if len(rsi) < 5:
                return False
            price_slope = closes[-1]  - closes[-6]
            rsi_slope   = rsi[-1]     - rsi[-6]
            return price_slope > 0 and rsi_slope < -3.0
        except Exception:
            return False

    @staticmethod
    def _volume_dried_up(df: Any, threshold: float = 0.5) -> bool:
        """Returns True if last candle volume < threshold × 20-bar average."""
        try:
            vols = df["volume"].astype(float).values
            if len(vols) < 21:
                return False
            avg = float(vols[-21:-1].mean())
            return avg > 0 and float(vols[-1]) < avg * threshold
        except Exception:
            return False

    @staticmethod
    def _hours_open(position: "Position") -> float:
        if not position.open_time:
            return 0.0
        try:
            opened = datetime.fromisoformat(position.open_time)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - opened).total_seconds() / 3600.0
        except Exception:
            return 0.0

    @staticmethod
    def _floating_pnl(position: "Position", price: float):
        """Returns (pnl_usdt, pnl_pct, r_multiple)."""
        entry    = position.effective_entry
        qty      = position.remaining_qty()
        risk     = entry - position.stop_loss
        pnl_usdt = (price - entry) * qty
        pnl_pct  = (price - entry) / entry * 100 if entry > 0 else 0.0
        r_mult   = (price - entry) / risk if risk > 0 else 0.0
        return pnl_usdt, pnl_pct, r_mult

    def _build_exit_prompt(
        self,
        symbol:    str,
        position:  "Position",
        price:     float,
        btc:       Dict[str, Any],
        hours:     float,
        vol_ratio: float,
        fg_val:    int,
    ) -> str:
        entry = position.effective_entry
        pnl_usdt, pnl_pct, r_mult = self._floating_pnl(position, price)
        sign = "+" if pnl_usdt >= 0 else ""
        return (
            f"Evaluate this open long position — decide HOLD / CLOSE / MOVE_SL / PARTIAL_CLOSE.\n\n"
            f"POSITION:\n"
            f"  Symbol     : {symbol}\n"
            f"  Entry      : ${entry:.6f}\n"
            f"  Current    : ${price:.6f}\n"
            f"  Stop Loss  : ${position.stop_loss:.6f}\n"
            f"  TP1/TP2/TP3: ${position.tp1:.4f} / ${position.tp2:.4f} / ${position.tp3:.4f}\n"
            f"  P&L        : {sign}${abs(pnl_usdt):.4f} USDT ({sign}{pnl_pct:.2f}%)\n"
            f"  R Multiple : {r_mult:.2f}R\n"
            f"  Zone       : {position.zone_type}\n"
            f"  Open time  : {hours:.1f} hours\n"
            f"  TP1 hit: {position.tp1_hit} | TP2 hit: {position.tp2_hit}\n"
            f"  Break-even: {position.break_even_active} | Trailing: {position.trailing_active}\n\n"
            f"MARKET:\n"
            f"  BTC 1H structure : {btc['bias']}\n"
            f"  BTC 1H change    : {btc['pct_change_1h'] * 100:+.2f}%\n"
            f"  Volume ratio     : {vol_ratio:.2f}x avg\n"
            f"  Fear & Greed     : {fg_val}\n\n"
            f"Apply strict ICT/SMC rules. Capital protection first."
        )

    def _analyze_exit_with_claude(self, symbol: str, prompt: str) -> Dict[str, Any]:
        """Call Claude for exit decision. Returns dict with decision/reason/new_sl."""
        default: Dict[str, Any] = {"decision": "HOLD", "reason": "Claude unavailable", "new_sl": None}
        for attempt in range(2):
            try:
                msg = self._client.messages.create(
                    model    = _CLAUDE_MODEL,
                    max_tokens = 256,
                    system   = _EXIT_SYSTEM_PROMPT,
                    messages = [{"role": "user", "content": prompt}],
                )
                raw = msg.content[0].text.strip()
                if "```" in raw:
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                r = json.loads(raw.strip())
                return {
                    "decision": r.get("decision", "HOLD"),
                    "reason":   r.get("reason", ""),
                    "new_sl":   r.get("new_sl"),
                }
            except anthropic.APIError as exc:
                log.warning("[SmartExit] Claude API error for %s (%d/2): %s", symbol, attempt + 1, exc)
                if attempt == 0:
                    time.sleep(5)
            except json.JSONDecodeError:
                return default
            except Exception as exc:
                log.warning("[SmartExit] Unexpected Claude error for %s (%d/2): %s", symbol, attempt + 1, exc)
                if attempt == 0:
                    time.sleep(5)
        return default

    # ---------------------------------------------------------------- #
    #  Smart exit — main logic                                          #
    # ---------------------------------------------------------------- #

    def _execute_smart_close(
        self,
        symbol:    str,
        position:  "Position",
        price:     float,
        reason:    str,
        pnl_usdt:  float,
        pnl_pct:   float,
        tick_size: float = 0.0,
    ) -> None:
        """Cancel OCO → market sell remaining → record PnL → notify."""
        try:
            self._risk_mgr.cancel_oco_for_position(symbol, self._api)
            remaining = position.remaining_qty() or position.quantity
            self._api.place_market_sell(symbol, remaining)
            self._risk_mgr.record_closed_pnl(pnl_usdt)
            self._risk_mgr.remove_position(symbol)
            log.info(
                "[SmartExit] %s CLOSED — %s | PnL=%.4f USDT (%.2f%%)",
                symbol, reason, pnl_usdt, pnl_pct,
            )
            tg.smart_exit(symbol, reason, "Closed position", pnl_usdt, pnl_pct)
            self._reset_daily_counters_if_new_day()
            if pnl_usdt >= 0:
                self._daily_wins   += 1
            else:
                self._daily_losses += 1
        except MEXCAPIError as exc:
            log.error("[SmartExit] API error closing %s: %s", symbol, exc)
        except Exception as exc:
            log.exception("[SmartExit] Unexpected error closing %s: %s", symbol, exc)

    def _execute_partial_smart_close(
        self,
        symbol:    str,
        position:  "Position",
        price:     float,
        reason:    str,
        pnl_usdt:  float,
        pnl_pct:   float,
        tick_size: float = 0.0,
    ) -> None:
        """Sell 50% of remaining qty, refresh OCO for the rest."""
        try:
            remaining = position.remaining_qty() or position.quantity
            half      = round(remaining * 0.5, 8)
            if half <= 0:
                return
            self._risk_mgr.cancel_oco_for_position(symbol, self._api)
            self._api.place_market_sell(symbol, half)
            partial_pnl = (price - position.effective_entry) * half
            self._risk_mgr.record_closed_pnl(partial_pnl)
            position.quantity = max(round(position.quantity - half, 8), 0.0)
            self._risk_mgr.save_positions()
            next_tp = position.tp2 if position.tp1_hit else position.tp1
            if next_tp > 0 and position.quantity > 0:
                self._risk_mgr.place_oco_for_position(
                    symbol, self._api, next_tp, position.stop_loss, tick_size
                )
            log.info(
                "[SmartExit] %s PARTIAL CLOSE — sold %.6f (50%%) | partial_pnl=%.4f",
                symbol, half, partial_pnl,
            )
            tg.smart_exit(symbol, reason, f"Closed 50%, holding {remaining - half:.6f}", partial_pnl, pnl_pct * 0.5)
        except MEXCAPIError as exc:
            log.error("[SmartExit] API error partial-closing %s: %s", symbol, exc)
        except Exception as exc:
            log.exception("[SmartExit] Unexpected error partial-closing %s: %s", symbol, exc)

    def _smart_exit_one(
        self,
        symbol:   str,
        position: "Position",
        btc:      Dict[str, Any],
        fg_val:   int,
    ) -> None:
        """Run all smart-exit logic for a single open position."""
        ws = self._ws_feed
        try:
            price = (ws.get_price(symbol) if ws else None) or self._api.get_ticker_price(symbol)
        except Exception:
            return

        entry     = position.effective_entry
        hours     = self._hours_open(position)
        pnl_usdt, pnl_pct, r_mult = self._floating_pnl(position, price)
        risk      = entry - position.stop_loss
        sym_info  = self._ensure_sym_info(symbol)
        tick_size = sym_info.get("tick_size", 0.0)

        # ── 1. Market deterioration — immediate closes ────────────────
        close_reason: Optional[str] = None
        if btc["bias"] == "BEARISH":
            close_reason = "BTC structure flipped BEARISH"
        elif btc["pct_change_1h"] < -0.02:
            close_reason = f"BTC dropped {btc['pct_change_1h'] * 100:.1f}% in 1 hour"
        elif fg_val < 25:
            close_reason = f"Extreme Fear (F&G={fg_val})"

        if close_reason:
            log.info("[SmartExit] %s — market deterioration: %s", symbol, close_reason)
            self._execute_smart_close(symbol, position, price, close_reason, pnl_usdt, pnl_pct, tick_size)
            return

        # ── 2. Per-symbol technical checks ───────────────────────────
        df: Any            = None
        sym_bias: str      = "UNKNOWN"
        vol_ratio: float   = 1.0
        tech_reason: Optional[str] = None
        claude_trigger: Optional[str] = None

        try:
            klines = self._api.get_klines(symbol, "60m", 50)
            if klines:
                df       = candles_to_df(klines)
                sym_bias = detect_market_structure(df).get("bias", "UNKNOWN")
                vols     = df["volume"].astype(float).values
                avg_v    = float(vols[-21:-1].mean()) if len(vols) > 20 else float(vols.mean())
                vol_ratio = float(vols[-1]) / avg_v if avg_v > 0 else 1.0

                # Immediate: structure flipped BEARISH (CHoCH proxy)
                if sym_bias == "BEARISH":
                    tech_reason = "CHoCH — 1H structure flipped BEARISH"

                # Immediate: price broke below entry zone
                elif float(df.iloc[-1]["close"]) < entry * 0.997:
                    tech_reason = "Price broke below OB/FVG entry zone"

                # Claude evaluation: RSI divergence
                elif self._rsi_divergence(df):
                    claude_trigger = "Bearish RSI divergence on 1H"

                # Claude evaluation: volume dry + non-bullish structure
                elif self._volume_dried_up(df) and sym_bias != "BULLISH":
                    claude_trigger = f"Volume dry-up ({vol_ratio:.2f}x avg) + structure {sym_bias}"

        except Exception as exc:
            log.debug("[SmartExit] Technical check error for %s: %s", symbol, exc)

        if tech_reason:
            log.info("[SmartExit] %s — technical: %s", symbol, tech_reason)
            self._execute_smart_close(symbol, position, price, tech_reason, pnl_usdt, pnl_pct, tick_size)
            return

        # ── 3. Profit protection (SL adjustments, no close) ──────────
        if risk > 0 and not position.break_even_active and r_mult >= 1.5:
            be_sl = round(entry * 1.001, 8)
            if be_sl > position.stop_loss:
                position.stop_loss        = be_sl
                position.break_even_active = True
                self._risk_mgr.save_positions()
                next_tp = position.tp2 if position.tp1_hit else position.tp1
                if next_tp > 0:
                    self._risk_mgr.cancel_oco_for_position(symbol, self._api)
                    self._risk_mgr.place_oco_for_position(symbol, self._api, next_tp, be_sl, tick_size)
                log.info("[SmartExit] %s — break-even SL set at %.6f (%.2fR)", symbol, be_sl, r_mult)
                tg.position_update(symbol, "MOVE_SL", f"Break-even at {r_mult:.2f}R profit", pnl_usdt, pnl_pct)
                return

        if risk > 0 and position.break_even_active and not position.trailing_active and r_mult >= 2.0:
            position.trailing_active = True
            self._risk_mgr.save_positions()
            log.info("[SmartExit] %s — aggressive trailing activated (%.2fR)", symbol, r_mult)
            tg.position_update(symbol, "MOVE_SL", f"Aggressive trailing activated at {r_mult:.2f}R", pnl_usdt, pnl_pct)
            return

        # ── 4. Time-based / ambiguous → Claude ────────────────────────
        if hours > 8:
            claude_trigger = claude_trigger or f"Position open {hours:.1f}h — evaluate continuation"

        if btc["volume_ratio"] < 0.5 and not claude_trigger:
            claude_trigger = f"BTC volume very low ({btc['volume_ratio']:.2f}x avg)"

        if claude_trigger:
            prompt   = self._build_exit_prompt(symbol, position, price, btc, hours, vol_ratio, fg_val)
            result   = self._analyze_exit_with_claude(symbol, prompt)
            decision = result["decision"]
            reason   = result["reason"]
            log.info("[SmartExit] %s — Claude: %s | %s", symbol, decision, reason)

            if decision == "CLOSE":
                self._execute_smart_close(
                    symbol, position, price, f"Claude: {reason}", pnl_usdt, pnl_pct, tick_size
                )
                return

            elif decision == "PARTIAL_CLOSE":
                self._execute_partial_smart_close(
                    symbol, position, price, f"Claude: {reason}", pnl_usdt, pnl_pct, tick_size
                )
                return

            elif decision == "MOVE_SL":
                raw_sl = result.get("new_sl")
                if raw_sl:
                    try:
                        new_sl = round(float(raw_sl), 8)
                        if new_sl > position.stop_loss:
                            position.stop_loss = new_sl
                            self._risk_mgr.save_positions()
                            next_tp = position.tp2 if position.tp1_hit else position.tp1
                            if next_tp > 0:
                                self._risk_mgr.cancel_oco_for_position(symbol, self._api)
                                self._risk_mgr.place_oco_for_position(symbol, self._api, next_tp, new_sl, tick_size)
                            log.info("[SmartExit] %s — SL raised to %.6f (Claude)", symbol, new_sl)
                    except (ValueError, TypeError):
                        pass
                tg.position_update(symbol, "MOVE_SL", reason, pnl_usdt, pnl_pct)

            else:  # HOLD
                tg.position_update(symbol, "HOLD", reason, pnl_usdt, pnl_pct)

        else:
            log.info(
                "[SmartExit] %s — HOLD | %.2fR | BTC:%s | F&G:%d | hrs:%.1f",
                symbol, r_mult, btc["bias"], fg_val, hours,
            )

    def _check_smart_exits(self) -> None:
        """
        Run intelligent exit analysis for every open position.
        Called every _SMART_EXIT_INTERVAL seconds from the main loop.
        Fetches shared BTC + F&G context once, then analyses each position.
        """
        positions = self._risk_mgr.all_positions()
        if not positions:
            return
        log.info("[SmartExit] Running check for %d position(s).", len(positions))
        btc    = self._btc_market_check()
        try:
            fg_val = int(news_filter.get_crypto_sentiment().get("fear_greed", 50))
        except Exception:
            fg_val = 50
        for symbol, position in list(positions.items()):
            try:
                self._smart_exit_one(symbol, position, btc, fg_val)
            except Exception as exc:
                log.exception("[SmartExit] Unhandled error for %s: %s", symbol, exc)

    # ---------------------------------------------------------------- #
    #  Claude integration                                               #
    # ---------------------------------------------------------------- #

    def _build_signal_prompt(
        self,
        signal: TradeSignal,
        market_context: Any,
        positions_count: int,
        daily_pnl_pct: float,
        crypto_sentiment: Optional[Dict[str, Any]] = None,
    ) -> str:
        risk        = signal.entry_price - signal.stop_loss
        rr_ratio    = round((signal.tp3 - signal.entry_price) / risk, 2) if risk > 0 else 0.0
        btc_dom     = getattr(market_context, "btc_dominance",   "N/A")
        mkt_change  = getattr(market_context, "market_change_pct", "N/A")
        fear_greed  = getattr(market_context, "fear_greed",      "N/A")
        btc_bias    = getattr(market_context, "btc_bias",        "N/A")

        confluence = []
        if signal.liquidity_sweep:     confluence.append("Liquidity Sweep")
        if signal.displacement:        confluence.append("Displacement")
        if signal.fvg_present:         confluence.append("FVG")
        if signal.ob_present:          confluence.append("Order Block")
        if signal.ote_zone:            confluence.append("OTE 62-79%")
        if signal.confirmation_candle: confluence.append("Confirmation Candle")
        if signal.vwap_filter:         confluence.append("VWAP aligned")

        breakdown_lines = "\n".join(
            f"    {k}: {v}/1" for k, v in signal.score_breakdown.items()
        ) if signal.score_breakdown else "    (not available)"

        # News & sentiment section
        if crypto_sentiment:
            sent_str   = (
                f"{crypto_sentiment['sentiment']} "
                f"(score: {crypto_sentiment['score']:+.2f})"
            )
            news_lines = "\n".join(
                f"    - {h}" for h in crypto_sentiment.get("top_news", [])
            ) or "    (none available)"
        else:
            sent_str   = "N/A"
            news_lines = "    (none available)"

        return f"""Analyze this trade signal and decide BUY or NO_TRADE.

SIGNAL DATA:
  Symbol        : {signal.symbol}
  Score         : {signal.score}/10
  Zone type     : {signal.zone_type}
  Kill zone     : {signal.kill_zone}
  HTF structure : {signal.structure}
  Entry price   : {signal.entry_price:.6f}
  Stop loss     : {signal.stop_loss:.6f} ({(risk / signal.entry_price * 100):.2f}% risk)
  TP1 (1:1)     : {signal.tp1:.6f}
  TP2 (2:1)     : {signal.tp2:.6f}
  TP3 (3:1)     : {signal.tp3:.6f}
  RR ratio      : 1:{rr_ratio}
  Confluence    : {', '.join(confluence) or 'None detected'}
  Discount zone : {signal.discount_zone}

SCORE BREAKDOWN:
{breakdown_lines}

MARKET CONTEXT:
  BTC dominance      : {btc_dom}%
  Global 24h change  : {mkt_change}%
  Fear & Greed index : {fear_greed}
  BTC bias           : {btc_bias}

NEWS & SENTIMENT:
  Crypto sentiment : {sent_str}
  Recent headlines :
{news_lines}

ACCOUNT STATUS:
  Open positions : {positions_count}/{config.MAX_OPEN_POSITIONS}
  Daily P&L      : {daily_pnl_pct:+.2f}% (cap: -{config.DAILY_LOSS_CAP_PCT * 100:.0f}%)

Make your decision based on STRICT ICT/SMC rules.
Respond ONLY in the required JSON format."""

    def analyze_signal_with_claude(
        self,
        signal: TradeSignal,
        market_context: Any,
        positions_count: int,
        daily_pnl_pct: float,
        crypto_sentiment: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Sends a signal to Claude and returns the parsed decision dict, or None
        if the API is unavailable after one retry.

        Return keys: decision, confidence, reason, risk_level, invalidation
        """
        prompt = self._build_signal_prompt(
            signal, market_context, positions_count, daily_pnl_pct, crypto_sentiment
        )

        for attempt in range(2):
            try:
                message = self._client.messages.create(
                    model=_CLAUDE_MODEL,
                    max_tokens=512,
                    system=_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw_text = message.content[0].text.strip()

                # Extract JSON even if Claude wraps it in markdown code fences
                if "```" in raw_text:
                    raw_text = raw_text.split("```")[1]
                    if raw_text.startswith("json"):
                        raw_text = raw_text[4:]

                return json.loads(raw_text.strip())

            except anthropic.APIError as exc:
                log.warning(
                    "[Claude] API error on %s (attempt %d/2): %s",
                    signal.symbol, attempt + 1, exc,
                )
                if attempt == 0:
                    time.sleep(5)

            except json.JSONDecodeError as exc:
                log.warning(
                    "[Claude] Could not parse JSON response for %s: %s",
                    signal.symbol, exc,
                )
                return None

            except Exception as exc:
                log.error(
                    "[Claude] Unexpected error for %s (attempt %d/2): %s",
                    signal.symbol, attempt + 1, exc,
                )
                if attempt == 0:
                    time.sleep(5)

        log.warning(
            "[Claude] API unavailable for %s — signal skipped (fallback: run main.py).",
            signal.symbol,
        )
        return None

    # ---------------------------------------------------------------- #
    #  Logging                                                          #
    # ---------------------------------------------------------------- #

    def _log_decision(self, symbol: str, response: Dict[str, Any], trade_type: str = "") -> None:
        """Append one line to claude_decisions.log."""
        ts         = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        decision   = response.get("decision", "UNKNOWN")
        confidence = response.get("confidence", 0)
        reason     = response.get("reason", "")
        risk_lvl   = response.get("risk_level", "")
        _decision_log.info(
            "[%s] %s | TradeType: %s | Decision: %s | Confidence: %s | Risk: %s | Reason: %s",
            ts, symbol, trade_type or "unknown", decision, confidence, risk_lvl, reason,
        )

    def _log_trade(
        self,
        signal: TradeSignal,
        response: Dict[str, Any],
        fill_price: float,
        quantity: float,
        order_id: str,
    ) -> None:
        """Append executed trade record to trade-journal.json."""
        risk = signal.entry_price - signal.stop_loss
        record = {
            "timestamp":          datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol":             signal.symbol,
            "signal_score":       signal.score,
            "signal_zone":        signal.zone_type,
            "kill_zone":          signal.kill_zone,
            "structure":          signal.structure,
            "entry_price":        signal.entry_price,
            "stop_loss":          signal.stop_loss,
            "tp1":                signal.tp1,
            "tp2":                signal.tp2,
            "tp3":                signal.tp3,
            "rr_ratio":           round((signal.tp3 - signal.entry_price) / risk, 2) if risk > 0 else 0,
            "claude_decision":    response.get("decision"),
            "claude_confidence":  response.get("confidence"),
            "claude_reason":      response.get("reason"),
            "claude_risk_level":  response.get("risk_level"),
            "claude_invalidation": response.get("invalidation"),
            "fill_price":         fill_price,
            "quantity":           quantity,
            "order_id":           order_id,
        }
        journal = _load_journal()
        journal.append(record)
        _save_journal(journal)

    # ---------------------------------------------------------------- #
    #  Trade execution                                                  #
    # ---------------------------------------------------------------- #

    def _execute_entry(self, signal: TradeSignal) -> Optional[float]:
        """
        Size and execute a market buy. Returns cost in USDT or None on failure.
        Always fetches a fresh balance immediately before sizing so the 1% risk
        is calculated against the real current equity.
        """
        symbol    = signal.symbol
        sym_info  = self._ensure_sym_info(symbol)
        is_friday = signal.kill_zone == "FRIDAY_REDUCED"

        # Fresh balance for accurate percentage-based sizing
        try:
            fresh_balance = self._api.get_usdt_balance()
            self._balance = fresh_balance   # keep cached value in sync
        except MEXCAPIError as exc:
            log.error("[%s] Cannot fetch fresh balance for sizing: %s", symbol, exc)
            return None

        qty = self._risk_mgr.calculate_quantity(
            balance      = fresh_balance,
            entry_price  = signal.entry_price,
            stop_loss    = signal.stop_loss,
            qty_step     = sym_info["qty_step"],
            min_qty      = sym_info["min_qty"],
            min_notional = sym_info["min_notional"],
            is_friday    = is_friday,
        )
        if qty is None or qty <= 0:
            log.warning("[%s] Position size too small — skipped.", symbol)
            return None

        qty = _round_step(qty, sym_info["qty_step"], sym_info["base_precision"])

        try:
            order    = self._api.place_market_buy(symbol, qty)
            order_id = str(order.get("orderId", ""))

            executed_qty = float(order.get("executedQty") or qty)
            cumul_quote  = float(order.get("cummulativeQuoteQty") or 0)
            fill_price   = (
                cumul_quote / executed_qty
                if executed_qty > 0 and cumul_quote > 0
                else signal.entry_price
            )

            # Enforce 3% hard SL cap from fill price
            max_sl_dist = fill_price * 0.03
            safe_sl     = max(signal.stop_loss, fill_price - max_sl_dist, 0.0)
            risk        = fill_price - safe_sl

            if risk > 0 and abs(fill_price - signal.entry_price) / signal.entry_price > 0.001:
                tp1 = fill_price + risk * 1.0
                tp2 = fill_price + risk * 2.0
                tp3 = fill_price + risk * 3.0
            else:
                tp1 = signal.tp1 if signal.tp1 > 0 else fill_price + risk * 1.0
                tp2 = signal.tp2 if signal.tp2 > 0 else fill_price + risk * 2.0
                tp3 = signal.tp3 if signal.tp3 > 0 else fill_price + risk * 3.0

            position = Position(
                symbol      = symbol,
                side        = "BUY",
                entry_price = fill_price,
                quantity    = executed_qty,
                stop_loss   = safe_sl,
                take_profit = tp1,
                order_id    = order_id,
                zone_type   = signal.zone_type,
                fill_price  = fill_price,
                tp1         = tp1,
                tp2         = tp2,
                tp3         = tp3,
                open_time   = datetime.now(timezone.utc).isoformat(timespec="seconds"),
                score       = signal.score,
                entry_atr   = getattr(signal, "atr", 0.0),
            )
            self._risk_mgr.add_position(position)
            # Exchange-side crash protection: OCO covers TP1 and SL
            self._risk_mgr.place_oco_for_position(
                symbol, self._api, tp1, safe_sl,
                sym_info.get("tick_size", 0.0),
            )

            log.info(
                "[%s] BUY %.6f @ %.6f (fill) | SL=%.6f | TP1=%.6f TP2=%.6f TP3=%.6f "
                "| score=%d/10 | zone=%s | friday=%s",
                symbol, executed_qty, fill_price,
                safe_sl, tp1, tp2, tp3,
                signal.score, signal.zone_type, is_friday,
            )
            return executed_qty * fill_price, fill_price, executed_qty, order_id

        except MEXCAPIError as exc:
            log.error("[%s] Order rejected: %s", symbol, exc)
            return None

    def _handle_exits(self) -> None:
        """Check all open positions for SL/TP/trailing exits."""
        ws = self._ws_feed
        for symbol, position in list(self._risk_mgr.all_positions().items()):
            try:
                ws_price = ws.get_price(symbol) if ws else None
                price    = ws_price or self._api.get_ticker_price(symbol)

                if position.trailing_active and position.entry_atr > 0:
                    self._risk_mgr.update_trailing_stop(symbol, price, position.entry_atr)

                reason = self._risk_mgr.check_exit(symbol, price)
                if not reason:
                    continue

                current_pos = self._risk_mgr.get_position(symbol)
                if not current_pos:
                    continue

                log.info(
                    "[%s] EXIT: %s | price=%.6f | entry=%.6f",
                    symbol, reason, price, current_pos.effective_entry,
                )

                sym_info  = self._ensure_sym_info(symbol)
                tick_size = sym_info.get("tick_size", 0.0)

                if reason == "TP1":
                    # Cancel OCO first to prevent double-execution with the exchange order
                    self._risk_mgr.cancel_oco_for_position(symbol, self._api)
                    sell_qty = current_pos.partial_qty(1)
                    self._api.place_market_sell(symbol, sell_qty)
                    pnl = (price - current_pos.effective_entry) * sell_qty
                    self._risk_mgr.record_closed_pnl(pnl)
                    # handle_tp1_hit sets break-even SL and places new OCO (TP2/BE)
                    self._risk_mgr.handle_tp1_hit(symbol, self._api, tick_size)
                    log.info("[%s] TP1 hit — sold %.6f (33%%) | PnL=+%.4f USDT", symbol, sell_qty, pnl)
                    tg.tp_hit(symbol, 1, pnl, price)

                elif reason == "TP2":
                    # Cancel OCO first, handle_tp2_hit places new OCO (TP3/trailing SL)
                    self._risk_mgr.cancel_oco_for_position(symbol, self._api)
                    sell_qty = current_pos.partial_qty(2)
                    self._api.place_market_sell(symbol, sell_qty)
                    pnl = (price - current_pos.effective_entry) * sell_qty
                    self._risk_mgr.record_closed_pnl(pnl)
                    self._risk_mgr.handle_tp2_hit(symbol, self._api, tick_size)
                    log.info("[%s] TP2 hit — sold %.6f (33%%) | PnL=+%.4f USDT | trailing active", symbol, sell_qty, pnl)
                    tg.tp_hit(symbol, 2, pnl, price)

                else:
                    # Full close (SL, TP3, TAKE_PROFIT) — cancel OCO then market sell
                    self._risk_mgr.cancel_oco_for_position(symbol, self._api)
                    remaining = current_pos.remaining_qty() or current_pos.quantity
                    self._api.place_market_sell(symbol, remaining)
                    pnl = (price - current_pos.effective_entry) * remaining
                    self._risk_mgr.record_closed_pnl(pnl)
                    self._risk_mgr.remove_position(symbol)
                    log.info(
                        "[%s] Closed (%s). PnL=%.4f USDT | Daily=%.2f%% | Weekly=%.2f%%",
                        symbol, reason, pnl,
                        self._risk_mgr.daily_loss_pct(),
                        self._risk_mgr.weekly_loss_pct(),
                    )

                    # Update daily outcome counters
                    self._reset_daily_counters_if_new_day()
                    ref = self._risk_mgr._session_start_balance or self._balance or 1.0
                    pnl_pct = pnl / ref * 100

                    if pnl >= 0:
                        self._daily_wins += 1
                        tg.trade_closed(symbol, price, pnl, pnl_pct, reason, self._balance)
                    else:
                        self._daily_losses += 1
                        tg.sl_hit(symbol, abs(pnl), abs(pnl_pct), price)
                        tg.trade_closed(symbol, price, pnl, pnl_pct, reason, self._balance)

            except MEXCAPIError as exc:
                log.error("[%s] Error executing exit: %s", symbol, exc)

    # ---------------------------------------------------------------- #
    #  Daily summary                                                    #
    # ---------------------------------------------------------------- #

    def _reset_daily_counters_if_new_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Re-use the same sentinel to detect the day roll-over
        if not hasattr(self, "_counter_date"):
            self._counter_date: str = today
        if today != self._counter_date:
            self._daily_wins   = 0
            self._daily_losses = 0
            self._counter_date = today

    def _maybe_send_daily_summary(self) -> None:
        """Send the daily summary once per day at 23:55–23:59 UTC."""
        now   = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour != 23 or now.minute < 55:
            return
        if self._summary_sent_date == today:
            return   # already sent today

        trades = self._risk_mgr._daily_trades
        pnl    = self._risk_mgr.daily_pnl
        ref    = self._risk_mgr._session_start_balance or self._balance
        pnl_pct = (pnl / ref * 100) if ref > 0 else 0.0

        tg.daily_summary(
            trades   = trades,
            wins     = self._daily_wins,
            losses   = self._daily_losses,
            pnl_usdt = pnl,
            pnl_pct  = pnl_pct,
            balance  = self._balance,
        )
        self._summary_sent_date = today
        log.info("[ClaudeTrader] Daily summary sent to Telegram.")

    # ---------------------------------------------------------------- #
    #  Main loop                                                        #
    # ---------------------------------------------------------------- #

    def run(self) -> None:
        """
        Main 5-minute loop.

        Each iteration:
          1. Check and execute exits on all open positions.
          2. If outside kill zone → sleep and continue.
          3. Run protection filters + Claude review on each qualified signal.
          4. Execute approved entries.
        """
        # ── Startup ─────────────────────────────────────────────────────
        if not config.API_KEY or not config.API_SECRET:
            log.error("MEXC credentials missing — check .env for MEXC_API_KEY / MEXC_SECRET.")
            return

        try:
            server_time = self._api.get_server_time()
            log.info(
                "[ClaudeTrader] Connected to MEXC. Server time: %s",
                datetime.utcfromtimestamp(server_time / 1000).strftime("%Y-%m-%d %H:%M:%S UTC"),
            )
        except Exception as exc:
            log.error("[ClaudeTrader] MEXC connectivity check failed: %s", exc)
            return

        try:
            self._balance = self._api.get_usdt_balance()
            self._risk_mgr.set_session_balance(self._balance)
            log.info("[ClaudeTrader] Opening balance: %.2f USDT", self._balance)
        except Exception as exc:
            log.error("[ClaudeTrader] Cannot fetch balance: %s", exc)
            return

        log.info("[ClaudeTrader] Building initial pair list…")
        active_pairs = self._coin_sel.get_pairs()
        log.info("[ClaudeTrader] %d pairs: %s", len(active_pairs), active_pairs)

        uuid_map = {
            c.mexc_symbol: c.uuid
            for c in (self._coin_sel._coins or [])
            if c.uuid
        }

        self._market_ctx = MarketContextPoller()
        self._market_ctx.start()
        self._ws_feed = WSPriceFeed(uuid_map)
        self._ws_feed.start()

        self._cmd_handler.start()
        log.info("[ClaudeTrader] Background services started. Loop interval: %ds.", _LOOP_INTERVAL)

        # ── Cycle loop ──────────────────────────────────────────────────
        while True:
            try:
                loop_start = time.time()

                # Refresh pair list (auto-refreshes internally every 4h)
                active_pairs = self._coin_sel.get_pairs()
                uuid_map = {
                    c.mexc_symbol: c.uuid
                    for c in (self._coin_sel._coins or [])
                    if c.uuid
                }
                if uuid_map:
                    self._ws_feed.update_uuid_map(uuid_map)

                # Refresh balance
                try:
                    self._balance = self._api.get_usdt_balance()
                except MEXCAPIError as exc:
                    log.error("[ClaudeTrader] Balance fetch failed: %s", exc)
                    time.sleep(_LOOP_INTERVAL)
                    continue

                # ── Always: daily counter reset + summary check ─────────
                self._reset_daily_counters_if_new_day()
                self._maybe_send_daily_summary()

                # ── Always: check hard SL/TP exits ──────────────────────
                self._handle_exits()

                # ── Smart exit check (every 15 min) ──────────────────────
                now_ts = time.time()
                if now_ts - self._smart_exit_timer >= _SMART_EXIT_INTERVAL:
                    self._smart_exit_timer = now_ts
                    if self._risk_mgr.open_position_count() > 0:
                        self._check_smart_exits()

                # ── Gate 1: Weekend check (only hard block) ──────────────
                kill_zone = detect_kill_zone()
                if kill_zone is None:
                    log.info("[Diag] Kill zone: weekend ✗ (no trading Sat/Sun)")
                    time.sleep(_LOOP_INTERVAL)
                    continue
                if kill_zone == "OFF_HOURS":
                    log.info("[Diag] Kill zone: OFF_HOURS — scanning anyway (no bonus)")
                elif kill_zone == "FRIDAY_REDUCED":
                    log.info("[Diag] Kill zone: FRIDAY_REDUCED — half position size")
                else:
                    log.info("[Diag] Kill zone: %s ✓ (+1 bonus)", kill_zone)

                # ── Gate 2: Global market filter ─────────────────────────
                trade_type = self._determine_trade_type()
                ctx = self._market_ctx.get_context()
                market_ok = check_global_market(ctx, trade_type)
                if not market_ok["tradeable"]:
                    log.info("[Diag] Market: %s ✗", market_ok["reason"])
                    log.info(
                        "[ClaudeTrader][MarketFilter][%s] %s",
                        trade_type, market_ok["reason"],
                    )
                    btc_dom = getattr(ctx, "btc_dominance", None) if ctx else None
                    tg.market_filter(market_ok["reason"], btc_dom)
                    time.sleep(_LOOP_INTERVAL)
                    continue

                _diag_dom = getattr(ctx, "btc_dominance", 0.0) if ctx else 0.0
                _diag_fg  = getattr(ctx, "fear_greed",    "N/A") if ctx else "N/A"
                log.info(
                    "[Diag] Market: BTC dom=%.1f%% %s | F&G=%s ✓",
                    _diag_dom, trade_type, _diag_fg,
                )

                altcoin_restricted = self._market_ctx.is_altcoin_restricted()

                # ── Gate 2b: News filter ──────────────────────────────────
                news_check = news_filter.is_news_time()
                if news_check["block"]:
                    log.info(
                        "[ClaudeTrader][NewsFilter] Blocking: %s",
                        news_check["reason"],
                    )
                    tg.news_block(
                        event     = news_check["event"],
                        reason    = news_check["reason"],
                        block_min = news_filter.BLOCK_WINDOW_MIN,
                    )
                    time.sleep(_LOOP_INTERVAL)
                    continue

                # ── Gate 3: Manual pause via /stop command ───────────────
                if not self._cmd_handler.trading_enabled:
                    log.info("[ClaudeTrader] Trading paused via /stop — skipping entry scan.")
                    time.sleep(_LOOP_INTERVAL)
                    continue

                # ── Gate 4: Risk manager global limits ───────────────────
                if self._risk_mgr.daily_loss_cap_reached():
                    log.warning("[ClaudeTrader] Daily loss cap reached — no new entries.")
                    time.sleep(_LOOP_INTERVAL)
                    continue

                if not self._risk_mgr.can_open_position(""):
                    log.info("[ClaudeTrader] Global position limits — pausing entry scan.")
                    time.sleep(_LOOP_INTERVAL)
                    continue

                # ── Sentiment snapshot for Claude context ─────────────────
                crypto_sentiment = news_filter.get_crypto_sentiment()
                log.info(
                    "[ClaudeTrader][Sentiment] %s (score=%.2f) | top: %s",
                    crypto_sentiment["sentiment"],
                    crypto_sentiment["score"],
                    "; ".join(crypto_sentiment["top_news"][:2]) or "none",
                )

                # ── Phase 1: Collect qualifying signals from full universe ─
                self._scan_stats = {k: 0 for k in (
                    "dex_spike", "atr", "correlation", "position_limit",
                    "order_book", "structure", "no_signal", "low_score",
                    "low_strength", "no_zone", "price_far", "approved",
                )}
                qualifying: List[TradeSignal] = []
                checked = 0

                for symbol in active_pairs:
                    coin_score = self._coin_sel.get_quality_score(symbol)
                    if coin_score < config.MIN_COIN_SCORE:
                        continue
                    checked += 1
                    sig = self._evaluate_coin_signal(symbol, trade_type=trade_type)
                    if sig is not None:
                        qualifying.append(sig)

                # ── Phase 2: Rank by score; send top N to Claude ──────────
                qualifying.sort(key=lambda s: s.score, reverse=True)
                candidates = qualifying[:_MAX_CLAUDE_CANDIDATES]

                # ── Scan summary + top-blocker ────────────────────────────
                _s = self._scan_stats
                _no_sig_total = (
                    _s.get("no_signal", 0) +
                    _s.get("low_score", 0) +
                    _s.get("low_strength", 0)
                )
                _scan_summary = (
                    f"{checked} checked | "
                    f"{_s.get('atr', 0)} ATR fail | "
                    f"{_s.get('structure', 0)} structure fail | "
                    f"{_no_sig_total} no/low signal | "
                    f"{_s.get('approved', 0)} approved → Claude: {len(candidates)}"
                )
                log.info("[Scan Summary] %s", _scan_summary)

                _blockers = {
                    "ATR":          _s.get("atr", 0),
                    "Correlation":  _s.get("correlation", 0),
                    "OrderBook":    _s.get("order_book", 0),
                    "4H Structure": _s.get("structure", 0),
                    "No Signal":    _s.get("no_signal", 0),
                    "Low Score":    _s.get("low_score", 0),
                    "Low Strength": _s.get("low_strength", 0),
                    "No Zone":      _s.get("no_zone", 0),
                    "Price Far":    _s.get("price_far", 0),
                }
                _top_name = max(_blockers, key=lambda k: _blockers[k])
                _top_n    = _blockers[_top_name]
                self._last_scan_summary     = _scan_summary
                self._last_scan_top_blocker = (
                    f"{_top_name} ({_top_n} coins)" if _top_n > 0 else "none"
                )

                log.info(
                    "[Scan] %d coins checked → %d active signals → top %d sent to Claude",
                    checked, len(qualifying), len(candidates),
                )

                entries_this_cycle = 0
                max_entries        = 1 if altcoin_restricted else 2
                positions_count    = self._risk_mgr.open_position_count()
                daily_pnl_pct      = -self._risk_mgr.daily_loss_pct()

                for signal in candidates:
                    if entries_this_cycle >= max_entries:
                        break

                    symbol = signal.symbol

                    # Re-check position constraints (state changes after each execution)
                    corr_check = check_correlation_guard(symbol, self._risk_mgr.all_positions())
                    if not corr_check["allowed"]:
                        log.info("[%s] Correlation (re-check): %s", symbol, corr_check["reason"])
                        continue
                    if not self._risk_mgr.can_open_position(symbol):
                        continue

                    log.info(
                        "[%s] → Claude: score=%d/10 zone=%s kill=%s "
                        "trade_type=%s entry=%.6f SL=%.6f TP3=%.6f",
                        symbol, signal.score, signal.zone_type,
                        signal.kill_zone, trade_type, signal.entry_price,
                        signal.stop_loss, signal.tp3,
                    )

                    try:
                        response = self.analyze_signal_with_claude(
                            signal, ctx, positions_count, daily_pnl_pct,
                            crypto_sentiment,
                        )

                        if response is None:
                            log.warning("[%s] Claude unavailable — skipping.", symbol)
                            continue

                        self._log_decision(symbol, response, trade_type)

                        decision   = response.get("decision", "NO_TRADE")
                        confidence = int(response.get("confidence", 0))

                        if decision != "BUY" or confidence < 8:
                            log.info(
                                "[%s] Claude: %s (confidence=%d) — %s",
                                symbol, decision, confidence,
                                response.get("reason", ""),
                            )
                            tg.no_trade(
                                symbol,
                                response.get("reason", "Claude rejected signal"),
                                signal.score,
                            )
                            continue

                        # ── Execute ───────────────────────────────────────
                        log.info(
                            "[%s] Claude APPROVED (confidence=%d, risk=%s) — executing.",
                            symbol, confidence, response.get("risk_level", ""),
                        )
                        result = self._execute_entry(signal)
                        if result:
                            cost, fill_price, quantity, order_id = result
                            self._balance -= cost
                            entries_this_cycle += 1
                            self._log_trade(signal, response, fill_price, quantity, order_id)

                            risk = fill_price - signal.stop_loss
                            rr   = round((signal.tp3 - fill_price) / risk, 1) if risk > 0 else 3
                            tg.trade_opened(
                                symbol    = symbol,
                                entry     = fill_price,
                                sl        = signal.stop_loss,
                                tp1       = signal.tp1,
                                tp2       = signal.tp2,
                                tp3       = signal.tp3,
                                score     = signal.score,
                                rr        = rr,
                                kill_zone = signal.kill_zone,
                            )

                    except MEXCAPIError as exc:
                        log.error("[%s] API error during Claude evaluation: %s", symbol, exc)
                    except Exception as exc:
                        log.exception("[%s] Unexpected error during evaluation: %s", symbol, exc)

                elapsed   = time.time() - loop_start
                sleep_for = max(0.0, _LOOP_INTERVAL - elapsed)
                log.info(
                    "[ClaudeTrader] Cycle done in %.1fs | "
                    "pairs=%d | positions=%d | entries=%d | sleeping %.0fs",
                    elapsed, len(active_pairs),
                    self._risk_mgr.open_position_count(),
                    entries_this_cycle, sleep_for,
                )
                time.sleep(sleep_for)

            except KeyboardInterrupt:
                log.info("[ClaudeTrader] Shutdown requested — stopping services.")
                self._cmd_handler.stop()
                if self._ws_feed:
                    self._ws_feed.stop()
                if self._market_ctx:
                    self._market_ctx.stop()
                break

            except Exception as exc:
                log.exception("[ClaudeTrader] Unhandled exception in main loop: %s", exc)
                tg.system_error(str(exc))
                time.sleep(30)


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    trader = ClaudeTrader()
    trader.run()
