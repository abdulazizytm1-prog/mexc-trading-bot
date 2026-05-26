"""
news_filter.py — High-impact news guard for the trading bot.

Three sources are checked before every scan cycle:

  1. ForexFactory economic calendar (nfs.faireconomy.media) — USD High-impact
     macro events.  Trading is blocked BLOCK_WINDOW_MIN minutes before and
     after each event.

  2. CoinDesk RSS + Cointelegraph RSS — free crypto news feeds.  Used for:
       a. Hard block when a high-impact keyword appears in a headline published
          within the last BLOCK_WINDOW_MIN minutes.
       b. Sentiment scoring (keyword polarity) passed to Claude as context.

  3. Fear & Greed Index (alternative.me/fng) — numeric market sentiment score
     blended into the sentiment output.

All network calls are cached and fail-open: if a source is unreachable the
filter returns block=False so trading is never halted by an API outage.
"""

from __future__ import annotations

import email.utils
import logging
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

# ── Endpoints ─────────────────────────────────────────────────────────────────

_FOREX_CAL_URL    = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_COINDESK_RSS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"
_CT_RSS_URL       = "https://cointelegraph.com/rss"
_FEAR_GREED_URL   = "https://api.alternative.me/fng/?limit=1"

# ── Timing ────────────────────────────────────────────────────────────────────

BLOCK_WINDOW_MIN    = 15     # block N minutes BEFORE and AFTER a high-impact event
_HTTP_TIMEOUT       =  8     # seconds per HTTP request
_CALENDAR_CACHE_TTL = 3600   # re-fetch economic calendar once per hour
_RSS_CACHE_TTL      =  600   # re-fetch RSS feeds every 10 minutes
_FG_CACHE_TTL       = 1800   # re-fetch Fear & Greed every 30 minutes

# ── Keyword sets ──────────────────────────────────────────────────────────────

# ForexFactory high-impact USD events that block trading
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

# RSS headline substrings that trigger a hard block (all lowercase, checked with `in`)
_RSS_BLOCK_KEYWORDS: Tuple[str, ...] = (
    # Macro
    "interest rate",
    "fed rate",
    "fomc",
    " cpi ",
    "nonfarm payroll",
    "nfp report",
    # Regulatory / exchange crisis
    "sec ban",
    "sec rejects",
    "sec approves",
    "sec bitcoin",
    "sec ethereum",
    "crypto ban",
    "bitcoin ban",
    "exchange hack",
    "exchange crash",
    "exchange collapse",
    "exchange bankrupt",
    "major hack",
    # Market shock
    "massive liquidation",
    "flash crash",
    "etf rejected",
    "etf denial",
    "etf approved",
    "etf approval",
)

_BULLISH_KEYWORDS: Tuple[str, ...] = (
    "approved", "approval", "etf inflow", "record high", "bullish",
    "adoption", "institutional", "rally", "surge", "breakout",
    "all-time high", "ath", "partnership", "launch", "upgrade",
    "accumulation",
)

_BEARISH_KEYWORDS: Tuple[str, ...] = (
    "hack", "crash", "ban", "reject", "rejected", "dump",
    "collapse", "lawsuit", "fraud", "scam", "exploit",
    "vulnerability", "bankrupt", "crackdown", "seized",
    "criminal", "arrest", "shutdown", "halted", "liquidation",
)

# ── Thread-safe caches ────────────────────────────────────────────────────────

_lock: threading.Lock = threading.Lock()

_calendar_cache:    Optional[Tuple[float, List[Dict[str, Any]]]] = None
_rss_cache:         Optional[Tuple[float, List[Dict[str, Any]]]] = None
_fear_greed_cache:  Optional[Tuple[float, Dict[str, Any]]]       = None

# ── RSS helpers ───────────────────────────────────────────────────────────────

_RSS_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TradingBot/1.0; "
        "https://github.com/example/mexc-trading-bot)"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _parse_rss_date(date_str: str) -> Optional[datetime]:
    """
    Parse an RSS pubDate string into a UTC-aware datetime.
    Handles RFC 2822 ("Mon, 26 May 2025 10:00:00 +0000") and ISO-8601.
    Returns None on any parse failure.
    """
    if not date_str:
        return None
    # RFC 2822 — standard RSS format
    try:
        return email.utils.parsedate_to_datetime(date_str.strip()).astimezone(timezone.utc)
    except Exception:
        pass
    # ISO-8601 fallback (some feeds use this)
    try:
        return datetime.fromisoformat(date_str.strip().replace("Z", "+00:00"))
    except Exception:
        return None


def _fetch_rss(url: str) -> List[Dict[str, Any]]:
    """
    Fetch one RSS feed and return a list of article dicts:
      {"title": str, "published_at": datetime | None, "link": str}
    Returns [] on any network or parse error (fail-open).
    """
    try:
        resp = requests.get(url, headers=_RSS_HEADERS, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        # Strip byte-order mark if present before parsing
        content = resp.content.lstrip(b"\xef\xbb\xbf")
        root = ET.fromstring(content)
    except Exception as exc:
        log.warning("[NewsFilter] Cannot fetch RSS %s: %s", url, exc)
        return []

    articles: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        title   = (item.findtext("title")   or "").strip()
        link    = (item.findtext("link")    or "").strip()
        pub_str = (
            item.findtext("pubDate")
            or item.findtext("published")
            or ""
        ).strip()
        if title:
            articles.append({
                "title":        title,
                "link":         link,
                "published_at": _parse_rss_date(pub_str),
            })
    return articles


def _fetch_all_rss() -> List[Dict[str, Any]]:
    """
    Fetch and merge CoinDesk + Cointelegraph RSS feeds with a shared TTL cache.
    Returns the stale cache (or []) on total failure — never raises.
    """
    global _rss_cache
    now_ts = datetime.now(timezone.utc).timestamp()

    with _lock:
        if _rss_cache and now_ts - _rss_cache[0] < _RSS_CACHE_TTL:
            return _rss_cache[1]

    coindesk = _fetch_rss(_COINDESK_RSS_URL)
    ct       = _fetch_rss(_CT_RSS_URL)
    combined = coindesk + ct

    if not combined:
        log.warning("[NewsFilter] Both RSS feeds empty/unreachable — using stale cache.")
        with _lock:
            return _rss_cache[1] if _rss_cache else []

    # Sort newest-first; articles without a parsed date go to the end
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    combined.sort(key=lambda a: a["published_at"] or _epoch, reverse=True)

    with _lock:
        _rss_cache = (now_ts, combined)

    log.info(
        "[NewsFilter] RSS refreshed — CoinDesk:%d  CT:%d  total:%d",
        len(coindesk), len(ct), len(combined),
    )
    return combined


# ── ForexFactory helpers ──────────────────────────────────────────────────────

def _is_us_dst(dt: datetime) -> bool:
    """Return True if dt (naive) falls within US Daylight Saving Time."""
    year      = dt.year
    mar8      = datetime(year, 3, 8)
    dst_start = mar8 + timedelta(days=(6 - mar8.weekday()) % 7)
    nov1      = datetime(year, 11, 1)
    dst_end   = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return dst_start <= dt.replace(tzinfo=None) < dst_end


def _parse_event_utc(date_str: str, time_str: str) -> Optional[datetime]:
    """Convert a ForexFactory date/time pair to a UTC-aware datetime."""
    if not time_str or time_str.lower() in ("all day", "tentative", ""):
        return None
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


# ── Fear & Greed ──────────────────────────────────────────────────────────────

def _fetch_fear_greed() -> Dict[str, Any]:
    """
    Fetch the Crypto Fear & Greed Index from alternative.me.

    Returns {"value": int, "label": str}.
    Falls back to {"value": 50, "label": "Neutral"} on any error.
    """
    global _fear_greed_cache
    now_ts = datetime.now(timezone.utc).timestamp()
    with _lock:
        if _fear_greed_cache and now_ts - _fear_greed_cache[0] < _FG_CACHE_TTL:
            return _fear_greed_cache[1]
    try:
        resp = requests.get(_FEAR_GREED_URL, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        entry  = resp.json()["data"][0]
        result = {
            "value": int(entry.get("value", 50)),
            "label": entry.get("value_classification", "Neutral"),
        }
    except Exception as exc:
        log.warning("[NewsFilter] Cannot fetch Fear & Greed: %s", exc)
        with _lock:
            return _fear_greed_cache[1] if _fear_greed_cache else {"value": 50, "label": "Neutral"}
    with _lock:
        _fear_greed_cache = (now_ts, result)
    log.info("[NewsFilter] Fear & Greed: %d (%s)", result["value"], result["label"])
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def is_news_time() -> Dict[str, Any]:
    """
    Check whether a high-impact news event falls within the blocking window.

    Check order:
      1. ForexFactory USD High-impact events (±BLOCK_WINDOW_MIN minutes).
      2. RSS headlines — any _RSS_BLOCK_KEYWORDS match published in the last
         BLOCK_WINDOW_MIN minutes triggers a block.

    Fails open — returns block=False if all sources are unreachable.

    Returns
    -------
    {
      "block"  : bool,
      "reason" : str,
      "event"  : str,
      "time"   : str,
    }
    """
    now = datetime.now(timezone.utc)

    # ── 1. Economic calendar ──────────────────────────────────────────
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
            display  = ev.get("title") or ev.get("name") or "High-impact event"
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

    # ── 2. RSS headlines — last BLOCK_WINDOW_MIN minutes ─────────────
    cutoff = now - timedelta(minutes=BLOCK_WINDOW_MIN)
    for article in _fetch_all_rss():
        pub = article.get("published_at")
        if pub is None:
            continue
        if pub < cutoff:
            # Articles are sorted newest-first; once we pass the cutoff we're done
            break
        title_lower = article["title"].lower()
        matched = next((kw for kw in _RSS_BLOCK_KEYWORDS if kw in title_lower), None)
        if matched:
            headline = article["title"][:80]
            pub_str  = pub.strftime("%H:%M UTC")
            log.info(
                "[NewsFilter] RSS block — keyword=%r  headline=%s  pub=%s",
                matched, headline, pub_str,
            )
            return {
                "block":  True,
                "reason": f"NEWS: {headline}",
                "event":  headline,
                "time":   pub_str,
            }

    return {"block": False, "reason": "", "event": "", "time": ""}


def get_crypto_sentiment() -> Dict[str, Any]:
    """
    Score overall crypto market sentiment from RSS headlines + Fear & Greed index.

    Methodology:
      - Fear & Greed index provides a [-1, +1] base score:
          value 0–100  →  (value - 50) / 50
      - RSS keyword polarity adjusts ±1 per article (first match wins).
      - Final blend: 60 % Fear & Greed + 40 % RSS polarity.
      - Clamped to [-1.0, +1.0].

    Returns
    -------
    {
      "sentiment"  : "BULLISH" | "BEARISH" | "NEUTRAL",
      "score"      : float in [-1.0, 1.0],
      "top_news"   : list[str]   (up to 5 recent headlines),
      "fear_greed" : int         (0–100 index value),
    }
    """
    fg    = _fetch_fear_greed()
    fg_v  = fg["value"]
    fg_score = (fg_v - 50.0) / 50.0   # 75 → +0.5,  25 → -0.5

    articles = _fetch_all_rss()
    bullish  = 0.0
    bearish  = 0.0
    headlines: List[str] = []

    for art in articles[:20]:
        title = art.get("title") or ""
        if title:
            headlines.append(title)
        tl = title.lower()
        for kw in _BULLISH_KEYWORDS:
            if kw in tl:
                bullish += 1.0
                break
        for kw in _BEARISH_KEYWORDS:
            if kw in tl:
                bearish += 1.0
                break

    rss_total = bullish + bearish
    rss_score = (bullish - bearish) / rss_total if rss_total > 0 else 0.0

    # 60 % Fear & Greed weight, 40 % RSS keyword weight
    score = round(fg_score * 0.6 + rss_score * 0.4, 2)
    score = max(-1.0, min(1.0, score))

    if   score >=  0.2: sentiment = "BULLISH"
    elif score <= -0.2: sentiment = "BEARISH"
    else:               sentiment = "NEUTRAL"

    return {
        "sentiment":  sentiment,
        "score":      score,
        "top_news":   headlines[:5],
        "fear_greed": fg_v,
    }
