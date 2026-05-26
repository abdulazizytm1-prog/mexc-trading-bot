"""Live test of the new news_filter.py — run: python test_news.py"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import news_filter
from datetime import datetime, timezone

now = datetime.now(timezone.utc)
print(f"{'='*60}")
print(f"news_filter live test  {now.strftime('%Y-%m-%d %H:%M UTC')}")
print(f"{'='*60}\n")

# ── 1. RSS feeds ──────────────────────────────────────────────────────
print("[TEST 1] Fetching RSS feeds...")
articles = news_filter._fetch_all_rss()
print(f"  Total articles fetched: {len(articles)}")
if articles:
    print("  5 most recent headlines:")
    for a in articles[:5]:
        pub = a["published_at"].strftime("%H:%M UTC") if a["published_at"] else "no date"
        print(f"    [{pub}] {a['title'][:70]}")
else:
    print("  WARNING: No articles returned — check connectivity")

# ── 2. Fear & Greed ───────────────────────────────────────────────────
print("\n[TEST 2] Fear & Greed Index...")
fg = news_filter._fetch_fear_greed()
print(f"  value={fg['value']}  label={fg['label']}")

# ── 3. Economic calendar ──────────────────────────────────────────────
print("\n[TEST 3] Economic calendar (USD High-impact)...")
cal = news_filter._fetch_calendar()
usd_high = [
    e for e in cal
    if (e.get("country") or e.get("currency") or "").upper() == "USD"
    and (e.get("impact") or "").capitalize() == "High"
]
print(f"  Total events: {len(cal)}  |  USD High-impact: {len(usd_high)}")
for ev in usd_high[:3]:
    print(f"    {ev.get('date')} {ev.get('time')}  {ev.get('title') or ev.get('name')}")

# ── 4. is_news_time() ─────────────────────────────────────────────────
print("\n[TEST 4] is_news_time()...")
result = news_filter.is_news_time()
print(f"  block  = {result['block']}")
print(f"  reason = {result['reason'] or '(none)'}")
print(f"  event  = {result['event']  or '(none)'}")
print(f"  time   = {result['time']   or '(none)'}")

# ── 5. get_crypto_sentiment() ─────────────────────────────────────────
print("\n[TEST 5] get_crypto_sentiment()...")
sent = news_filter.get_crypto_sentiment()
print(f"  sentiment  = {sent['sentiment']}")
print(f"  score      = {sent['score']:.2f}  (Fear&Greed={sent['fear_greed']})")
print(f"  top_news:")
for h in sent["top_news"]:
    print(f"    - {h[:72]}")

print(f"\n{'='*60}")
print("All tests complete.")
