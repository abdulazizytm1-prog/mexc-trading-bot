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
import threading
import time as _time
from typing import TYPE_CHECKING, Optional

import requests

if TYPE_CHECKING:
    from claude_trader import ClaudeTrader

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


def smart_exit(
    symbol:   str,
    reason:   str,
    action:   str,
    pnl_usdt: float,
    pnl_pct:  float,
) -> bool:
    """
    ⚠️ SMART EXIT: SYMBOL  (rule-triggered or Claude-triggered close / partial)
    """
    sign = "+" if pnl_usdt >= 0 else ""
    text = (
        f"⚠️ SMART EXIT: {symbol}\n"
        f"Reason: {reason}\n"
        f"Action: {action}\n"
        f"P&L: {sign}${abs(pnl_usdt):.2f} ({sign}{pnl_pct:.2f}%)"
    )
    return _send(text)


def position_update(
    symbol:   str,
    decision: str,
    reason:   str,
    pnl_usdt: float,
    pnl_pct:  float,
) -> bool:
    """
    📊 POSITION UPDATE: SYMBOL  (Claude decision: HOLD / MOVE_SL)
    """
    emoji = {"CLOSE": "🔴", "PARTIAL_CLOSE": "⚠️", "MOVE_SL": "🛡️"}.get(decision, "📊")
    sign  = "+" if pnl_usdt >= 0 else ""
    text = (
        f"{emoji} POSITION UPDATE: {symbol}\n"
        f"Claude decision: {decision}\n"
        f"Reason: {reason}\n"
        f"Current P&L: {sign}${abs(pnl_usdt):.2f} ({sign}{pnl_pct:.2f}%)"
    )
    return _send(text)


def news_block(event: str, reason: str, block_min: int = 30) -> bool:
    """
    ⚠️ NEWS FILTER ACTIVE  (sent when a high-impact news event blocks entries)
    """
    text = (
        f"⚠️ NEWS FILTER ACTIVE\n"
        f"Event: {event}\n"
        f"{reason}\n"
        f"Trading paused {block_min} min"
    )
    return _send(text)


# ------------------------------------------------------------------ #
#  Interactive command handler                                         #
# ------------------------------------------------------------------ #

class TelegramCommandHandler:
    """
    Background daemon thread that long-polls Telegram getUpdates and
    dispatches bot commands from the authorised chat.

    Long-polling (timeout=8 s on the Telegram side) means the server holds
    the connection open until a message arrives, so responses are near-instant
    instead of waiting up to _ERROR_BACKOFF seconds.

    Security
    --------
    Messages from any chat other than TELEGRAM_CHAT_ID are silently dropped.

    trading_enabled
    ---------------
    Public bool — read it in the main ClaudeTrader loop to gate new entries.
    /stop sets it False; /start sets it True.
    """

    _LONG_POLL_TIMEOUT = 8    # Telegram server-side wait (seconds)
    _HTTP_TIMEOUT      = 14   # requests timeout — must exceed _LONG_POLL_TIMEOUT
    _ERROR_BACKOFF     = 10   # seconds to sleep after a network/API error

    def __init__(self, trader: "ClaudeTrader") -> None:
        self.trading_enabled: bool = True
        self._trader            = trader
        self._offset:      int  = 0
        self._stop_event        = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- #
    #  Lifecycle                                                         #
    # ---------------------------------------------------------------- #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            log.info("[TelegramCmd] Listener already running — skipping start.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="TelegramCmdHandler",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "[TelegramCmd] Command listener started in thread '%s' "
            "(long-poll timeout=%ds).",
            self._thread.name, self._LONG_POLL_TIMEOUT,
        )
        _send("🤖 Bot started and ready!")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self._HTTP_TIMEOUT + 2)
        log.info("[TelegramCmd] Command listener stopped.")

    # ---------------------------------------------------------------- #
    #  Poll loop                                                         #
    # ---------------------------------------------------------------- #

    def _poll_loop(self) -> None:
        """
        Loop forever, long-polling Telegram for updates.

        With long polling the server holds the connection open for up to
        _LONG_POLL_TIMEOUT seconds, so an update returns immediately when it
        arrives — no fixed inter-poll sleep needed.  We only sleep on errors
        to avoid hammering the API.
        """
        log.info("[TelegramCmd] Poll loop running.")
        while not self._stop_event.is_set():
            try:
                self._fetch_and_dispatch()
            except Exception as exc:
                log.warning("[TelegramCmd] Unhandled poll error: %s", exc)
                self._stop_event.wait(self._ERROR_BACKOFF)
        log.info("[TelegramCmd] Poll loop exited.")

    def _fetch_and_dispatch(self) -> None:
        if not _TOKEN or not _CHAT_ID:
            # Credentials not configured — back off so we don't spin
            self._stop_event.wait(self._ERROR_BACKOFF)
            return
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{_TOKEN}/getUpdates",
                params={
                    "offset":  self._offset,
                    "limit":   20,
                    "timeout": self._LONG_POLL_TIMEOUT,
                },
                timeout=self._HTTP_TIMEOUT,
            )
        except requests.Timeout:
            # Long-poll expired with no updates — normal, just retry immediately
            return
        except Exception as exc:
            log.warning("[TelegramCmd] getUpdates network error: %s", exc)
            self._stop_event.wait(self._ERROR_BACKOFF)
            return

        if not resp.ok:
            log.warning(
                "[TelegramCmd] getUpdates HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            self._stop_event.wait(self._ERROR_BACKOFF)
            return

        for update in resp.json().get("result", []):
            update_id = update.get("update_id", 0)
            self._offset = update_id + 1          # advance past this update

            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue

            # Security: only respond to the configured chat
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if chat_id != str(_CHAT_ID):
                log.warning(
                    "[TelegramCmd] Ignored message from unknown chat %s "
                    "(expected %s).", chat_id, _CHAT_ID,
                )
                continue

            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                cmd_word = text.split()[0]
                log.info("[TelegramCmd] ← %s from chat %s", cmd_word, chat_id)
                self._dispatch(text)

    def _dispatch(self, text: str) -> None:
        # Strip @BotUsername suffix (e.g. /balance@mybot → /balance)
        cmd = text.split()[0].split("@")[0].lower()
        handlers = {
            "/balance":   self._cmd_balance,
            "/positions": self._cmd_positions,
            "/status":    self._cmd_status,
            "/stop":      self._cmd_stop,
            "/start":     self._cmd_start,
            "/report":    self._cmd_report,
            "/pairs":     self._cmd_pairs,
            "/help":      self._cmd_help,
        }
        fn = handlers.get(cmd)
        if fn:
            try:
                reply = fn()
                ok = _send(reply)
                log.info(
                    "[TelegramCmd] → %s reply sent (ok=%s, %d chars)",
                    cmd, ok, len(reply),
                )
            except Exception as exc:
                log.error("[TelegramCmd] Error in %s handler: %s", cmd, exc)
                _send(f"⚠️ Error running {cmd}: {exc}")
        else:
            _send(f"❓ Unknown command: {cmd}\nSend /help to see all commands.")

    # ---------------------------------------------------------------- #
    #  Command implementations                                           #
    # ---------------------------------------------------------------- #

    def _live_price(self, symbol: str) -> Optional[float]:
        """Try WS feed first, fall back to REST."""
        ws = getattr(self._trader, "_ws_feed", None)
        if ws:
            p = ws.get_price(symbol)
            if p:
                return p
        try:
            return self._trader._api.get_ticker_price(symbol)
        except Exception:
            return None

    def _cmd_balance(self) -> str:
        try:
            balance = self._trader._api.get_usdt_balance()
            self._trader._balance = balance

            # Total equity = free USDT + unrealised value of open positions
            total_equity = balance
            for sym, pos in self._trader._risk_mgr.all_positions().items():
                price = self._live_price(sym)
                if price:
                    total_equity += price * pos.remaining_qty()
                else:
                    total_equity += pos.effective_entry * pos.remaining_qty()

            return (
                f"💼 Balance: {_fmt_price(balance)} USDT\n"
                f"📊 Total equity: {_fmt_price(total_equity)}"
            )
        except Exception as exc:
            return f"⚠️ Could not fetch balance: {exc}"

    def _cmd_positions(self) -> str:
        positions = self._trader._risk_mgr.all_positions()
        if not positions:
            return "📊 Open Positions: 0\nNo open trades"

        lines = [f"📊 Open Positions: {len(positions)}\n"]
        for sym, pos in positions.items():
            entry = pos.effective_entry
            price = self._live_price(sym)
            if price and entry > 0:
                pnl_pct   = (price - entry) / entry * 100
                unrealised = (price - entry) * pos.remaining_qty()
                sign  = "+" if unrealised >= 0 else ""
                emoji = "✅" if unrealised >= 0 else "🔴"
                lines.append(
                    f"{emoji} {sym}\n"
                    f"   Entry: {_fmt_price(entry)}\n"
                    f"   Now:   {_fmt_price(price)}\n"
                    f"   PnL:   {sign}{pnl_pct:.2f}% ({sign}${abs(unrealised):.4f})\n"
                    f"   SL: {_fmt_price(pos.stop_loss)}"
                )
            else:
                lines.append(
                    f"📊 {sym}\n"
                    f"   Entry: {_fmt_price(entry)}\n"
                    f"   SL: {_fmt_price(pos.stop_loss)}"
                )
        return "\n".join(lines)

    def _cmd_status(self) -> str:
        status = "✅ RUNNING" if self.trading_enabled else "⏸ PAUSED"

        btc_line = "📈 BTC Dominance: N/A"
        ctx_obj = getattr(self._trader, "_market_ctx", None)
        if ctx_obj:
            ctx = ctx_obj.get_context()
            if ctx:
                dom = ctx.btc_dominance
                tag = " (restricted)" if dom > 55.0 else " (ok)"
                btc_line = f"📈 BTC Dominance: {dom:.1f}%{tag}"

        # Import here to avoid module-level circular dep
        from strategy import detect_kill_zone as _dkz
        kz = _dkz()
        kz_line = f"⏰ Kill Zone: {kz}" if kz else "⏰ Kill Zone: Outside session"

        last_summary = getattr(self._trader, "_last_scan_summary",     "")
        top_blocker  = getattr(self._trader, "_last_scan_top_blocker", "")
        scan_line    = f"\n📊 Last scan: {last_summary}"    if last_summary else ""
        blocker_line = f"\n🔍 Top blocker: {top_blocker}" if top_blocker  else ""

        return (
            f"🤖 Bot Status: {status}\n"
            f"{btc_line}\n"
            f"{kz_line}\n"
            f"🔄 Next scan: ~5 min"
            f"{scan_line}"
            f"{blocker_line}"
        )

    def _cmd_stop(self) -> str:
        self.trading_enabled = False
        log.warning("[TelegramCmd] Trading PAUSED via /stop command.")
        return (
            "⏸ Trading PAUSED\n"
            "Bot will keep monitoring open positions.\n"
            "Send /start to resume new entries."
        )

    def _cmd_start(self) -> str:
        self.trading_enabled = True
        log.info("[TelegramCmd] Trading RESUMED via /start command.")
        return "✅ Trading RESUMED\nBot will now scan for new entries."

    def _cmd_report(self) -> str:
        rm      = self._trader._risk_mgr
        trades  = rm._daily_trades
        wins    = getattr(self._trader, "_daily_wins",   0)
        losses  = getattr(self._trader, "_daily_losses", 0)
        pnl     = rm.daily_pnl
        ref     = rm._session_start_balance or getattr(self._trader, "_balance", 0) or 1.0
        pnl_pct = pnl / ref * 100
        win_rate = (wins / trades * 100) if trades > 0 else 0.0
        sign = "+" if pnl >= 0 else ""
        return (
            f"📅 Today's Report\n"
            f"Trades: {trades} | Wins: {wins} | Losses: {losses}\n"
            f"PnL: {sign}${abs(pnl):.2f} ({sign}{pnl_pct:.1f}%)\n"
            f"Win Rate: {win_rate:.0f}%"
        )

    def _cmd_pairs(self) -> str:
        try:
            pairs = self._trader._coin_sel.get_pairs()
        except Exception as exc:
            return f"⚠️ Could not retrieve pairs: {exc}"
        if not pairs:
            return "📋 No active pairs loaded yet."
        pair_list = "\n".join(f"  {i + 1:>2}. {p}" for i, p in enumerate(pairs))
        return f"📋 Active Pairs ({len(pairs)}):\n{pair_list}"

    def _cmd_help(self) -> str:
        return (
            "🤖 Trading Bot Commands\n\n"
            "/balance   — USDT balance + total equity\n"
            "/positions — open trades with live P&L\n"
            "/status    — bot state, BTC dominance, kill zone\n"
            "/stop      — pause new entries\n"
            "/start     — resume new entries\n"
            "/report    — today's P&L summary\n"
            "/pairs     — active trading pairs list\n"
            "/help      — this message"
        )
