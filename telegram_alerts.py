"""
telegram_alerts.py — Telegram notification helpers for the trading bot.

All public functions are fire-and-forget: errors are logged but never
propagated, so a Telegram failure never interrupts trading operations.

Required environment variables
-------------------------------
  TELEGRAM_BOT_TOKEN  — bot token from @BotFather
  TELEGRAM_CHAT_ID    — chat or channel ID to post to
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

log = logging.getLogger(__name__)

_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_TIMEOUT = 10   # HTTP request timeout (seconds)


# ------------------------------------------------------------------ #
#  Core send                                                           #
# ------------------------------------------------------------------ #

def _send(text: str) -> bool:
    """
    POST a plain-text message to the configured Telegram chat.
    Returns True on success, False on any error. Never raises.
    """
    if not _TOKEN or not _CHAT_ID:
        log.debug("[Telegram] Not configured — skipping alert.")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            json={"chat_id": _CHAT_ID, "text": text},
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            log.warning("[Telegram] Send failed (%d): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.Timeout:
        log.warning("[Telegram] Request timed out.")
        return False
    except Exception as exc:
        log.warning("[Telegram] Unexpected error: %s", exc)
        return False


# ------------------------------------------------------------------ #
#  Formatting helpers                                                  #
# ------------------------------------------------------------------ #

def _fmt_price(price: float) -> str:
    """Human-readable price with dollar sign and commas."""
    if price >= 1_000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:,.4f}"
    else:
        return f"${price:.6f}"


def _fmt_pnl(pnl_usdt: float, pnl_pct: float) -> str:
    sign = "+" if pnl_usdt >= 0 else ""
    return f"{sign}${abs(pnl_usdt):.2f} ({sign}{pnl_pct:.2f}%)" if pnl_usdt >= 0 \
        else f"-${abs(pnl_usdt):.2f} ({pnl_pct:.2f}%)"


def _session_label(kill_zone: Optional[str]) -> str:
    return {
        "LONDON":          "London Session",
        "NEW_YORK":        "New York Session",
        "LONDON_CLOSE":    "London Close",
        "FRIDAY_REDUCED":  "Friday (half size)",
    }.get(kill_zone or "", "Active Session")


def _sl_pct(entry: float, sl: float) -> float:
    if entry <= 0:
        return 0.0
    return (sl - entry) / entry * 100.0


# ------------------------------------------------------------------ #
#  Alert functions                                                     #
# ------------------------------------------------------------------ #

def trade_opened(
    symbol:    str,
    entry:     float,
    sl:        float,
    tp1:       float,
    tp2:       float,
    tp3:       float,
    score:     int,
    rr:        float,
    kill_zone: Optional[str] = None,
) -> bool:
    """
    🟢 TRADE OPENED
    """
    sl_change = _sl_pct(entry, sl)
    text = (
        f"🟢 TRADE OPENED\n"
        f"📊 {symbol} | Score: {score}/10\n"
        f"💰 Entry: {_fmt_price(entry)}\n"
        f"🛑 SL: {_fmt_price(sl)} ({sl_change:.2f}%)\n"
        f"🎯 TP1: {_fmt_price(tp1)} | TP2: {_fmt_price(tp2)} | TP3: {_fmt_price(tp3)}\n"
        f"📈 RR: 1:{rr:.0f}\n"
        f"⏰ {_session_label(kill_zone)}"
    )
    return _send(text)


def trade_closed(
    symbol:     str,
    exit_price: float,
    pnl_usdt:   float,
    pnl_pct:    float,
    reason:     str,
    balance:    float,
) -> bool:
    """
    ✅ TRADE CLOSED — WIN  /  ❌ TRADE CLOSED — LOSS
    """
    won    = pnl_usdt >= 0
    header = "✅ TRADE CLOSED — WIN" if won else "❌ TRADE CLOSED — LOSS"
    pnl_str = _fmt_pnl(pnl_usdt, pnl_pct)
    text = (
        f"{header}\n"
        f"📊 {symbol}\n"
        f"💵 PnL: {pnl_str}\n"
        f"📤 Exit: {_fmt_price(exit_price)} ({reason})\n"
        f"💼 Balance: {_fmt_price(balance)}"
    )
    return _send(text)


def tp_hit(
    symbol:      str,
    tp_level:    int,
    partial_pnl: float,
    price:       float = 0.0,
) -> bool:
    """
    🎯 TP{N} HIT — symbol
    """
    pct_closed = {1: "33%", 2: "33%", 3: "34%"}.get(tp_level, "partial")
    sign = "+" if partial_pnl >= 0 else ""
    price_str = f"\n📤 Price: {_fmt_price(price)} ({pct_closed} closed)" if price > 0 else ""
    text = (
        f"🎯 TP{tp_level} HIT — {symbol}\n"
        f"💵 Partial PnL: {sign}${abs(partial_pnl):.4f} USDT"
        f"{price_str}"
    )
    return _send(text)


def sl_hit(
    symbol:     str,
    loss_usdt:  float,
    loss_pct:   float,
    exit_price: float = 0.0,
) -> bool:
    """
    🔴 STOP LOSS HIT — symbol
    """
    price_str = f"\n📤 Exit: {_fmt_price(exit_price)}" if exit_price > 0 else ""
    text = (
        f"🔴 STOP LOSS HIT — {symbol}\n"
        f"💵 Loss: -${abs(loss_usdt):.4f} USDT (-{abs(loss_pct):.2f}%)"
        f"{price_str}"
    )
    return _send(text)


def daily_summary(
    trades:   int,
    wins:     int,
    losses:   int,
    pnl_usdt: float,
    pnl_pct:  float,
    balance:  float,
) -> bool:
    """
    📅 DAILY SUMMARY (sent at ~23:59 UTC)
    """
    win_rate = (wins / trades * 100) if trades > 0 else 0.0
    pnl_str  = _fmt_pnl(pnl_usdt, pnl_pct)
    text = (
        f"📅 DAILY SUMMARY\n"
        f"📊 Trades: {trades} | Wins: {wins} | Losses: {losses}\n"
        f"💰 PnL: {pnl_str}\n"
        f"💼 Balance: {_fmt_price(balance)}\n"
        f"📈 Win Rate: {win_rate:.1f}%"
    )
    return _send(text)


def no_trade(
    symbol: str,
    reason: str,
    score:  int = 0,
) -> bool:
    """
    ⏸ NO TRADE — symbol  (sent when Claude explicitly rejects a qualified signal)
    """
    score_str = f"\nScore: {score}/10" if score > 0 else ""
    text = (
        f"⏸ NO TRADE — {symbol}"
        f"{score_str}\n"
        f"Reason: {reason}"
    )
    return _send(text)


def system_error(error_message: str) -> bool:
    """
    ⚠️ SYSTEM ERROR
    """
    text = (
        f"⚠️ SYSTEM ERROR\n"
        f"Error: {error_message[:300]}"
    )
    return _send(text)


def market_filter(reason: str, btc_dom: Optional[float] = None) -> bool:
    """
    🔴 MARKET FILTER  (sent when macro conditions block all entries)
    """
    dom_line = f"\nBTC dominance: {btc_dom:.1f}% (too high)" if btc_dom is not None else ""
    text = (
        f"🔴 MARKET FILTER"
        f"{dom_line}\n"
        f"{reason}"
    )
    return _send(text)
