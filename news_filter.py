"""
news_filter.py — High-impact news guard for the trading bot.

Two sources are checked before every scan cycle:
  1. ForexFactory economic calendar (nfs.faireconomy.media) — USD High-impact
     macro events.  Trading is blocked BLOCK_WINDOW_MIN minutes before and
     after each event.
  2. CryptoPanic free API — important crypto headlines.  Used for:
       a. Hard block on critical events (ETF decisions, exchange hacks, SEC).
       b. Sentiment scoring passed to Claude as additional context.

All network calls are cached and fail-open: if a source is unreachable the
filter returns block=False so trading is never halted by an API outage.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

# ── API endpoints ─────────────────────────────────────────────────────────────

_CRYPTOPANIC_URL = (
    "https://cryptopanic.com/api/v1/posts/"
    "?auth_token=free&kind=news&filter=important&public=true"
)
_FOREX_CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# ── Timing ────────────────────────────────────────────────────────────────────

BLOCK_WINDOW_MIN    = 30    # block N minutes BEFORE and AFTER high-impact event
_HTTP_TIMEOUT       =  8    # seconds per HTTP request
_CALENDAR_CACHE_TTL = 3600  # re-fetch economic calendar once per hour
_NEWS_CACHE_TTL     =  300  # re-fetch CryptoPanic every 5 minutes

# ── High-impact keyword sets ──────────────────────────────────────────────────

_ECON_BLOCK_KEYWORDS: frozenset = frozenset({
    "federal funds rate",
    "interest rate decision",
    "fomc",
    "cpi",
    "consumer price index",
    "non-farm payroll",
    "nonfarm payroll",
    "nonfarm payrolls",
    "nfp",
    "pce price index",
    "pce",
    "gdp",
})

# CryptoPanic titles that trigger a hard trading block
_CRYPTO_BLOCK_PATTERNS: Tuple[str, ...] = (
    "btc etf",
    "bitcoin etf",
    "ethereum etf",
    "eth etf",
    "sec decision",
    "sec approves",
    "sec rejects",
    "sec bitcoin",
    "sec ethereum",
    "exchange hack",
    "exchange crash",
    "exchange collapse",
    "exchange bankrupt",
    "major hack",
    "crypto ban",
    "bitcoin ban",
)

_BULLISH_PATTERNS: Tuple[str, ...] = (
    "approved", "approval", "etf inflow", "record high", "bullish",
    "adoption", "institutional", "rally", "surge", "breakout",
    "all-time high", "ath", "partnership", "launch", "upgrade",
    "accumulation",
)

_BEARISH_PATTERNS: Tuple[str, ...] = (
    "hack", "crash", "ban", "reject", "rejected", "dump",
    "collapse", "lawsuit", "fraud", "scam", "exploit",
    "vulnerability", "bankrupt", "crackdown", "seized",
    "criminal", "arrest", "shutdown", "halted",
)

# ── Thread-safe cache ─────────────────────────────────────────────────────────

_lock = threading.Lock()
_calendar_cache: Optional[Tuple[float, List[Dict[str, Any]]]] = None
_news_cache:     Optional[Tuple[float, List[Dict[str, Any]]]] = None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_us_dst(dt: datetime) -> bool:
    """Return True if dt (naive) falls within US Daylight Saving Time."""
    year = dt.year
    # DST starts: second Sunday of March
    mar8      = datetime(year, 3, 8)
    dst_start = mar8 + timedelta(days=(6 - mar8.weekday()) % 7)
    # DST ends: first Sunday of November
    nov1      = datetime(year, 11, 1)
    dst_end   = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return dst_start <= dt.replace(tzinfo=None) < dst_end


def _parse_event_utc(date_str: str, time_str: str) -> Optional[datetime]:
    """
    Convert a ForexFactory date/time pair to UTC-aware datetime.

    date_str examples : "2024-01-31", "01-31-2024"
    time_str examples : "2:00pm", "10:30am", "All Day", "Tentative"
    Times are US/Eastern — UTC-4 (EDT) or UTC-5 (EST) depending on DST.
    """
    if not time_str or time_str.lower() in ("all day", "tentative", ""):
        return None

    # Parse date — try common ForexFactory formats
    parsed_date = None
    for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"):
        try:
            parsed_date = datetime.strptime(date_str, fmt).date()
            break
        except ValueError:
            pass
    if parsed_date is None:
        log.debug("[NewsFilter] Cannot parse date: %r", date_str)
        return None

    # Parse time — e.g. "2:00pm", "10:30am"
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)", time_str.lower().strip())
    if not m:
        log.debug("[NewsFilter] Cannot parse time: %r", time_str)
        return None

    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    naive     = datetime(parsed_date.year, parsed_date.month, parsed_date.day, hour, minute)
    et_offset = timedelta(hours=4) if _is_us_dst(naive) else timedelta(hours=5)
    return (naive + et_offset).replace(tzinfo=timezone.utc)


def _fetch_calendar() -> List[Dict[str, Any]]:
    """Fetch and cache the ForexFactory calendar.  Returns stale or [] on error."""
    global _calendar_cache
    now_ts = datetime.now(timezone.utc).timestamp()

    with _lock:
        if _calendar_cache and now_ts - _calendar_cache[0] < _CALENDAR_CACHE_TTL:
            return _calendar_cache[1]

    try:
        resp = requests.get(_FOREX_CAL_URL, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data: List[Dict[str, Any]] = resp.json()
    except Exception as exc:
        log.warning("[NewsFilter] Cannot fetch economic calendar: %s", exc)
        with _lock:
            return _calendar_cache[1] if _calendar_cache else []

    with _lock:
        _calendar_cache = (now_ts, data)
    log.info("[NewsFilter] Economic calendar refreshed (%d events).", len(data))
    return data


def _fetch_cryptopanic() -> List[Dict[str, Any]]:
    """Fetch and cache important CryptoPanic posts.  Returns stale or [] on error."""
    global _news_cache
    now_ts = datetime.now(timezone.utc).timestamp()

    with _lock:
        if _news_cache and now_ts - _news_cache[0] < _NEWS_CACHE_TTL:
            return _news_cache[1]

    try:
        resp = requests.get(_CRYPTOPANIC_URL, timeout=_HTTP_TIMEOUT)
        if resp.status_code in (401, 403):
            log.warning(
                "[NewsFilter] CryptoPanic returned HTTP %d — "
                "free tier may require registration. Skipping.",
                resp.status_code,
            )
            return []
        resp.raise_for_status()
        posts: List[Dict[str, Any]] = resp.json().get("results", [])
    except Exception as exc:
        log.warning("[NewsFilter] Cannot fetch CryptoPanic news: %s", exc)
        with _lock:
            return _news_cache[1] if _news_cache else []

    with _lock:
        _news_cache = (now_ts, posts)
    log.info("[NewsFilter] CryptoPanic refreshed (%d posts).", len(posts))
    return posts


# ── Public API ────────────────────────────────────────────────────────────────

def is_news_time() -> Dict[str, Any]:
    """
    Check whether a high-impact news event falls within the blocking window.

    Checks economic calendar first, then CryptoPanic for critical crypto news.
    Fails open — returns block=False if both sources are unreachable.

    Returns
    -------
    {
      "block"  : bool,
      "reason" : str,   e.g. "Federal Funds Rate in 25 min"
      "event"  : str,   e.g. "Federal Funds Rate"        (empty when block=False)
      "time"   : str,   e.g. "14:00 UTC"                 (empty when block=False)
    }
    """
    now = datetime.now(timezone.utc)

    # ── 1. Economic calendar — USD High impact events ─────────────────────
    for ev in _fetch_calendar():
        country = (ev.get("country") or ev.get("currency") or "").upper()
        if country != "USD":
            continue
        if (ev.get("impact") or "").capitalize() != "High":
            continue

        title = (ev.get("title") or ev.get("name") or "").lower()
        if not any(kw in title for kw in _ECON_BLOCK_KEYWORDS):
            continue

        event_utc = _parse_event_utc(ev.get("date", ""), ev.get("time", ""))
        if event_utc is None:
            continue

        delta_min = (event_utc - now).total_seconds() / 60.0
        if -BLOCK_WINDOW_MIN <= delta_min <= BLOCK_WINDOW_MIN:
            display = ev.get("title") or ev.get("name") or "High-impact event"
            readable = (
                f"in {int(delta_min)} min"
                if delta_min >= 0
                else f"{int(-delta_min)} min ago (window active)"
            )
            log.info(
                "[NewsFilter] Blocking: %s %s (event @ %s)",
                display, readable, event_utc.strftime("%H:%M UTC"),
            )
            return {
                "block":  True,
                "reason": f"{display} {readable}",
                "event":  display,
                "time":   event_utc.strftime("%H:%M UTC"),
            }

    # ── 2. CryptoPanic — critical crypto events in the last 2 hours ───────
    cutoff = now - timedelta(hours=2)
    for post in _fetch_cryptopanic():
        raw_ts = post.get("published_at") or post.get("created_at") or ""
        try:
            pub = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if pub < cutoff:
            continue

        title_lower = (post.get("title") or "").lower()
        matched = next((p for p in _CRYPTO_BLOCK_PATTERNS if p in title_lower), None)
        if matched:
            headline = (post.get("title") or matched)[:80]
            log.info("[NewsFilter] Blocking on crypto news: %s", headline)
            return {
                "block":  True,
                "reason": f"Critical crypto news: {headline}",
                "event":  "Crypto Block Event",
                "time":   pub.strftime("%H:%M UTC"),
            }

    return {"block": False, "reason": "", "event": "", "time": ""}


def get_crypto_sentiment() -> Dict[str, Any]:
    """
    Score overall crypto market sentiment from the latest important headlines.

    Uses both vote counts (positive/negative/important/disliked) and keyword
    pattern matching on titles.

    Returns
    -------
    {
      "sentiment" : "BULLISH" | "BEARISH" | "NEUTRAL",
      "score"     : float in [-1.0, 1.0]   (positive = bullish),
      "top_news"  : list[str]              (up to 5 headlines),
    }
    """
    posts = _fetch_cryptopanic()
    if not posts:
        return {"sentiment": "NEUTRAL", "score": 0.0, "top_news": []}

    bullish = 0.0
    bearish = 0.0
    headlines: List[str] = []

    for post in posts[:20]:
        title = post.get("title") or ""
        if title:
            headlines.append(title)

        # Vote-based signal
        votes   = post.get("votes") or {}
        pos     = int(votes.get("positive",  0) or 0)
        neg     = int(votes.get("negative",  0) or 0)
        imp     = int(votes.get("important", 0) or 0)
        dis     = int(votes.get("disliked",  0) or 0)
        bullish += pos + imp * 0.5
        bearish += neg + dis * 0.5

        # Keyword-based signal (first match wins per post to avoid double-counting)
        title_lower = title.lower()
        for kw in _BULLISH_PATTERNS:
            if kw in title_lower:
                bullish += 1.0
                break
        for kw in _BEARISH_PATTERNS:
            if kw in title_lower:
                bearish += 1.0
                break

    total = bullish + bearish
    score = round((bullish - bearish) / total, 2) if total > 0 else 0.0

    if   score >=  0.2: sentiment = "BULLISH"
    elif score <= -0.2: sentiment = "BEARISH"
    else:               sentiment = "NEUTRAL"

    return {
        "sentiment": sentiment,
        "score":     score,
        "top_news":  headlines[:5],
    }
