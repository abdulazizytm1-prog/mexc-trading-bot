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
from typing import Any, Dict, Optional

import anthropic

import config
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

_JOURNAL_PATH   = Path(__file__).parent / "trade-journal.json"
_LOOP_INTERVAL  = 300   # 5 minutes between scans
_CLAUDE_MODEL   = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")


# ------------------------------------------------------------------ #
#  Claude system prompt                                                #
# ------------------------------------------------------------------ #

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

    # ---------------------------------------------------------------- #
    #  Internal helpers                                                 #
    # ---------------------------------------------------------------- #

    def _is_claude_session(self) -> bool:
        """London 07:00–10:00 UTC or NY 13:00–16:00 UTC only."""
        hour = datetime.now(timezone.utc).hour
        return (7 <= hour < 10) or (13 <= hour < 16)

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
    #  Claude integration                                               #
    # ---------------------------------------------------------------- #

    def _build_signal_prompt(
        self,
        signal: TradeSignal,
        market_context: Any,
        positions_count: int,
        daily_pnl_pct: float,
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
    ) -> Optional[Dict[str, Any]]:
        """
        Sends a signal to Claude and returns the parsed decision dict, or None
        if the API is unavailable after one retry.

        Return keys: decision, confidence, reason, risk_level, invalidation
        """
        prompt = self._build_signal_prompt(
            signal, market_context, positions_count, daily_pnl_pct
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

    def _log_decision(self, symbol: str, response: Dict[str, Any]) -> None:
        """Append one line to claude_decisions.log."""
        ts        = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        decision  = response.get("decision", "UNKNOWN")
        confidence = response.get("confidence", 0)
        reason    = response.get("reason", "")
        risk_lvl  = response.get("risk_level", "")
        _decision_log.info(
            "[%s] %s | Decision: %s | Confidence: %s | Risk: %s | Reason: %s",
            ts, symbol, decision, confidence, risk_lvl, reason,
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
        Mirrors the _handle_entry logic from main.py.
        """
        symbol    = signal.symbol
        sym_info  = self._ensure_sym_info(symbol)
        is_friday = signal.kill_zone == "FRIDAY_REDUCED"

        qty = self._risk_mgr.calculate_quantity(
            balance      = self._balance,
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

                if reason == "TP1":
                    sell_qty = current_pos.partial_qty(1)
                    self._api.place_market_sell(symbol, sell_qty)
                    pnl = (price - current_pos.effective_entry) * sell_qty
                    self._risk_mgr.record_closed_pnl(pnl)
                    self._risk_mgr.handle_tp1_hit(symbol)
                    log.info("[%s] TP1 hit — sold %.6f (33%%) | PnL=+%.4f USDT", symbol, sell_qty, pnl)

                elif reason == "TP2":
                    sell_qty = current_pos.partial_qty(2)
                    self._api.place_market_sell(symbol, sell_qty)
                    pnl = (price - current_pos.effective_entry) * sell_qty
                    self._risk_mgr.record_closed_pnl(pnl)
                    self._risk_mgr.handle_tp2_hit(symbol)
                    log.info("[%s] TP2 hit — sold %.6f (33%%) | PnL=+%.4f USDT | trailing active", symbol, sell_qty, pnl)

                else:
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

            except MEXCAPIError as exc:
                log.error("[%s] Error executing exit: %s", symbol, exc)

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

                # ── Always: check exits ──────────────────────────────────
                self._handle_exits()

                # ── Gate 1: Kill zone check ──────────────────────────────
                if not self._is_claude_session():
                    log.debug("[ClaudeTrader] Outside Claude session — sleeping.")
                    time.sleep(_LOOP_INTERVAL)
                    continue

                kill_zone = detect_kill_zone()
                if kill_zone is None:
                    log.debug("[ClaudeTrader] No active ICT kill zone — sleeping.")
                    time.sleep(_LOOP_INTERVAL)
                    continue

                # ── Gate 2: Global market filter ─────────────────────────
                ctx = self._market_ctx.get_context()
                market_ok = check_global_market(ctx)
                if not market_ok["tradeable"]:
                    log.info("[ClaudeTrader][MarketFilter] %s", market_ok["reason"])
                    time.sleep(_LOOP_INTERVAL)
                    continue

                altcoin_restricted = self._market_ctx.is_altcoin_restricted()

                # ── Gate 3: Risk manager global limits ───────────────────
                if self._risk_mgr.daily_loss_cap_reached():
                    log.warning("[ClaudeTrader] Daily loss cap reached — no new entries.")
                    time.sleep(_LOOP_INTERVAL)
                    continue

                if not self._risk_mgr.can_open_position(""):
                    log.info("[ClaudeTrader] Global position limits — pausing entry scan.")
                    time.sleep(_LOOP_INTERVAL)
                    continue

                # ── Scan pairs ───────────────────────────────────────────
                entries_this_cycle = 0
                max_entries = 1 if altcoin_restricted else 2

                for symbol in active_pairs:
                    if entries_this_cycle >= max_entries:
                        break

                    # Coin quality gate
                    coin_score = self._coin_sel.get_quality_score(symbol)
                    if coin_score < config.MIN_COIN_SCORE:
                        continue

                    # DEX spike gate
                    if self._ws_feed.is_dex_spike(symbol):
                        log.warning(
                            "[%s] DEX volume spike (%.0f%%) — skipped.",
                            symbol, self._ws_feed.get_dex_ratio(symbol) * 100,
                        )
                        continue

                    try:
                        klines = self._api.get_klines(
                            symbol, config.PRIMARY_TIMEFRAME, config.CANDLE_LIMIT
                        )
                        if not klines:
                            continue

                        ws_price      = self._ws_feed.get_price(symbol)
                        current_price = ws_price or float(klines[-1][4])

                        # ── a. ATR filter ────────────────────────────────
                        atr_check = check_atr_filter(klines, current_price)
                        if not atr_check["tradeable"]:
                            log.debug("[%s] ATR: %s", symbol, atr_check["reason"])
                            continue

                        # ── b. Correlation guard ─────────────────────────
                        corr_check = check_correlation_guard(
                            symbol, self._risk_mgr.all_positions()
                        )
                        if not corr_check["allowed"]:
                            log.info("[%s] Correlation: %s", symbol, corr_check["reason"])
                            continue

                        # ── c. Per-symbol position check ──────────────────
                        if not self._risk_mgr.can_open_position(symbol):
                            continue

                        # ── d. Order book liquidity ───────────────────────
                        ob_check = check_order_book(symbol, current_price, self._api)
                        if not ob_check["liquid_enough"]:
                            log.info("[%s] Order book: %s", symbol, ob_check["reason"])
                            continue

                        # ── e. 4H market structure ────────────────────────
                        htf_df = None
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
                            if structure.get("bias") != "BULLISH":
                                log.debug(
                                    "[%s] HTF bias=%s — skipped.",
                                    symbol, structure.get("bias", "UNKNOWN"),
                                )
                                continue

                        # ── f. Strategy signal ────────────────────────────
                        df = candles_to_df(klines)
                        if df.empty:
                            continue

                        signal = generate_signal(symbol, df, htf_df=htf_df)
                        if signal is None or signal.score < 8:
                            continue

                        if signal.strength < 0.8:
                            continue

                        log.info(
                            "[%s] Signal qualified: score=%d/10 zone=%s kill=%s "
                            "entry=%.6f SL=%.6f TP3=%.6f | sending to Claude…",
                            symbol, signal.score, signal.zone_type,
                            signal.kill_zone, signal.entry_price,
                            signal.stop_loss, signal.tp3,
                        )

                        # ── g. Claude review ──────────────────────────────
                        positions_count = self._risk_mgr.open_position_count()
                        daily_pnl_pct   = -self._risk_mgr.daily_loss_pct()

                        response = self.analyze_signal_with_claude(
                            signal, ctx, positions_count, daily_pnl_pct
                        )

                        if response is None:
                            log.warning(
                                "[%s] Claude unavailable — skipping (fallback: run main.py).",
                                symbol,
                            )
                            continue

                        self._log_decision(symbol, response)

                        decision   = response.get("decision", "NO_TRADE")
                        confidence = int(response.get("confidence", 0))

                        if decision != "BUY" or confidence < 8:
                            log.info(
                                "[%s] Claude: %s (confidence=%d) — %s",
                                symbol, decision, confidence,
                                response.get("reason", ""),
                            )
                            continue

                        # ── h. Execute ────────────────────────────────────
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

                    except MEXCAPIError as exc:
                        log.error("[%s] API error during scan: %s", symbol, exc)
                    except Exception as exc:
                        log.exception("[%s] Unexpected error during scan: %s", symbol, exc)

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
                if self._ws_feed:
                    self._ws_feed.stop()
                if self._market_ctx:
                    self._market_ctx.stop()
                break

            except Exception as exc:
                log.exception("[ClaudeTrader] Unhandled exception in main loop: %s", exc)
                time.sleep(30)


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    trader = ClaudeTrader()
    trader.run()
