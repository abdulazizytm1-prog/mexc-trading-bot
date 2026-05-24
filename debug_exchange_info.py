"""
Raw diagnostic for GET https://api.mexc.com/api/v3/exchangeInfo

Run BEFORE any other fix:
    python debug_exchange_info.py

Prints exactly what MEXC returns so we know the real field names and
status values — no wrapper code, no assumptions.
"""

import json
import requests

URL = "https://api.mexc.com/api/v3/exchangeInfo"

print(f"GET {URL} ...\n")
resp = requests.get(URL, timeout=30)
print(f"HTTP {resp.status_code}  ({len(resp.content):,} bytes)\n")

data = resp.json()

# ── Top-level structure ──────────────────────────────────────────────
print("=" * 60)
print("TOP-LEVEL TYPE:", type(data).__name__)

if isinstance(data, dict):
    keys = list(data.keys())
    print("TOP-LEVEL KEYS:", keys)
    for k, v in data.items():
        if not isinstance(v, (list, dict)):
            print(f"  {k}: {v!r}")
elif isinstance(data, list):
    print(f"Response IS a list with {len(data)} items")

# ── Find the symbols array (wherever it lives) ───────────────────────
print("\n" + "=" * 60)
symbols_list = None
symbols_key  = None

if isinstance(data, list):
    symbols_list = data
    symbols_key  = "<root>"
elif isinstance(data, dict):
    for candidate in ("symbols", "data", "result", "list", "items"):
        val = data.get(candidate)
        if isinstance(val, list) and val:
            symbols_list = val
            symbols_key  = candidate
            break

if symbols_list is None:
    print("ERROR: could not find a list anywhere in the response.")
    print("Full response (first 2000 chars):")
    print(json.dumps(data, indent=2)[:2000])
    raise SystemExit(1)

print(f"Symbols array at key '{symbols_key}': {len(symbols_list)} items")

# ── Inspect the first symbol ─────────────────────────────────────────
print("\n" + "=" * 60)
first = symbols_list[0]
print(f"FIRST ITEM TYPE: {type(first).__name__}")
if isinstance(first, dict):
    print("FIRST ITEM KEYS:", list(first.keys()))
    print("\nFIRST ITEM VALUES (filters omitted):")
    for k, v in first.items():
        if k != "filters":
            print(f"  {k!r}: {v!r}")
else:
    print("FIRST ITEM:", first)

# ── Status value distribution ────────────────────────────────────────
print("\n" + "=" * 60)
status_dist: dict = {}
for s in symbols_list:
    if isinstance(s, dict):
        sv = s.get("status", "<missing>")
        status_dist[sv] = status_dist.get(sv, 0) + 1
print("STATUS VALUE DISTRIBUTION:")
for sv, cnt in sorted(status_dist.items(), key=lambda x: -x[1]):
    print(f"  {sv!r:20s} → {cnt} symbols")

# ── quoteAsset distribution (top 15) ────────────────────────────────
print("\n" + "=" * 60)
quote_dist: dict = {}
for s in symbols_list:
    if isinstance(s, dict):
        q = s.get("quoteAsset", s.get("quoteCurrency", "<missing>"))
        quote_dist[q] = quote_dist.get(q, 0) + 1
top_quotes = sorted(quote_dist.items(), key=lambda x: -x[1])[:15]
print("QUOTEASSET DISTRIBUTION (top 15):")
for q, cnt in top_quotes:
    print(f"  {q!r:20s} → {cnt} symbols")

# ── How many USDT pairs exist under each status? ─────────────────────
print("\n" + "=" * 60)
usdt_by_status: dict = {}
for s in symbols_list:
    if not isinstance(s, dict):
        continue
    q = s.get("quoteAsset", s.get("quoteCurrency", ""))
    if q == "USDT":
        sv = s.get("status", "<missing>")
        usdt_by_status[sv] = usdt_by_status.get(sv, 0) + 1
print("USDT PAIRS BY STATUS:")
for sv, cnt in sorted(usdt_by_status.items(), key=lambda x: -x[1]):
    print(f"  status={sv!r:20s} → {cnt} USDT pairs")

# ── Sample 5 USDT pairs for visual inspection ────────────────────────
print("\n" + "=" * 60)
usdt_samples = [
    s for s in symbols_list
    if isinstance(s, dict)
    and s.get("quoteAsset", s.get("quoteCurrency", "")) == "USDT"
][:5]
print(f"SAMPLE USDT PAIRS ({len(usdt_samples)} shown):")
for s in usdt_samples:
    symbol = s.get("symbol", "?")
    status = s.get("status", "?")
    spot   = s.get("isSpotTradingAllowed", "<missing>")
    print(f"  symbol={symbol!r:15s}  status={status!r:12s}  isSpotTradingAllowed={spot!r}")

print("\n" + "=" * 60)
print("Copy the STATUS value shown above for USDT pairs into mexc_api.py")
print("Done.")
