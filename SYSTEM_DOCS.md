# MEXC ICT/SMC Trading Bot — Complete Technical Documentation

**Generated:** 2026-05-26  
**Version:** post-OCO + smart-exit release (commit cd583fe)

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [File Reference](#3-file-reference)
   - [config.py](#31-configpy)
   - [main.py](#32-mainpy)
   - [claude_trader.py](#33-claude_traderpy)
   - [strategy.py](#34-strategypy)
   - [risk_manager.py](#35-risk_managerpy)
   - [mexc_api.py](#36-mexc_apipy)
   - [coin_selector.py](#37-coin_selectorpy)
   - [coin_ranker.py](#38-coin_rankerpy)
   - [filters.py](#39-filterspy)
   - [news_filter.py](#310-news_filterpy)
   - [telegram_alerts.py](#311-telegram_alertspy)
   - [market_context.py](#312-market_contextpy)
   - [ws_price_feed.py](#313-ws_price_feedpy)
4. [Full System Flow (10 Steps)](#4-full-system-flow)
5. [Entry Gate Hierarchy](#5-entry-gate-hierarchy)
6. [Exit System](#6-exit-system)
7. [OCO Order Lifecycle](#7-oco-order-lifecycle)
8. [All Thresholds and Limits](#8-all-thresholds-and-limits)
9. [Persistent State Files](#9-persistent-state-files)
10. [Log Files](#10-log-files)
11. [Environment Variables](#11-environment-variables)
12. [Deployment Modes](#12-deployment-modes)

---

## 1. System Overview

A spot-only USDT trading bot for MEXC that uses the ICT/SMC (Inner Circle Trader / Smart Money Concepts) strategy framework.

**Two execution modes:**
- `main.py` — Automatic mode. Executes signals when strategy score ≥ 0.80 strength.
- `claude_trader.py` — AI-supervised mode. Every qualifying signal (score ≥ 8/10) is sent to Claude Sonnet for a second-opinion review. Only executes when Claude returns `BUY` with confidence ≥ 8.

**Key characteristics:**
- Long-only spot trades (no shorting, no leverage)
- Halal-filtered coin universe (no stablecoins, meme coins, lending protocols, gambling)
- Dynamic pair selection refreshed every 4 hours via Coinranking Professional API
- Exchange-side crash protection via OCO orders placed immediately at entry
- Intelligent 15-minute smart exit system powered by Claude
- Interactive Telegram control panel (8 commands)

---

## 2. Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                     claude_trader.py / main.py               │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ CoinSelector │  │MarketContext │  │  WSPriceFeed      │  │
│  │ (4h refresh) │  │Poller (30m)  │  │  (Coinranking WS) │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬──────────┘  │
│         │                 │                   │              │
│  ┌──────▼─────────────────▼───────────────────▼──────────┐  │
│  │              5-minute main loop                        │  │
│  │  1. check_exits()   — SL/TP/trailing                  │  │
│  │  2. smart_exits()   — Claude AI review (15-min)       │  │
│  │  3. filter chain    — ATR, corr, book, structure       │  │
│  │  4. generate_signal() — ICT/SMC strategy              │  │
│  │  5. Claude review   — BUY/NO_TRADE (confidence ≥ 8)   │  │
│  │  6. execute_entry() — market buy + OCO placement      │  │
│  └───────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
         │                    │                    │
    ┌────▼────┐         ┌─────▼────┐        ┌─────▼──────┐
    │ MEXC v3 │         │Anthropic │        │  Telegram  │
    │  REST   │         │  Claude  │        │  Alerts +  │
    │   API   │         │  Sonnet  │        │  Commands  │
    └─────────┘         └──────────┘        └────────────┘
```

---

## 3. File Reference

### 3.1 config.py

**Purpose:** Central configuration — all tuneable parameters in one place.  
**No trading logic. Imported by every other module.**

| Parameter | Value | Description |
|-----------|-------|-------------|
| `BASE_URL` | `https://api.mexc.com` | MEXC v3 REST base |
| `RECV_WINDOW` | `5000` | HMAC timestamp tolerance (ms) |
| `QUOTE_CURRENCY` | `USDT` | All pairs quoted in USDT |
| `PRIMARY_TIMEFRAME` | `60m` | Entry signal timeframe (MEXC uses "60m" not "1h") |
| `CANDLE_LIMIT` | `200` | Klines fetched per symbol |
| `HTF_TIMEFRAME` | `4h` | Higher-timeframe bias filter |
| `HTF_CANDLE_LIMIT` | `50` | HTF klines fetched |
| `COIN_SELECTOR_REFRESH_HOURS` | `4` | How often to rebuild trading universe |
| `COINRANKING_PAGES` | `2` | Pages × 100 = 200 coins scanned |
| `MIN_SELECTED_PAIRS` | `5` | Minimum pairs (supplements with fallbacks) |
| `MAX_SELECTED_PAIRS` | `50` | Hard cap on active trading pairs |
| `MIN_COIN_SCORE` | `7` | Coinranking quality gate (0–10) |
| `MIN_MARKET_CAP_USD` | `$500M` | Coin quality gate |
| `MIN_MEXC_24H_VOLUME_USD` | `$50M` | Coin quality gate |
| `MAX_CHANGE_PCT` | `20.0%` | Reject coins with extreme 24h move |
| `FVG_MIN_SIZE_PCT` | `0.10%` | Minimum FVG gap to qualify |
| `OB_MIN_IMPULSE_PCT` | `0.50%` | Minimum impulse for valid OB |
| `OB_LOOKBACK` | `30` | Candles scanned for OBs |
| `ATR_PERIOD` | `14` | ATR calculation period |
| `ATR_SL_MULT` | `1.5` | SL = zone_edge − ATR × 1.5 |
| `MIN_SIGNAL_STRENGTH` | `0.25` | Reject signals below this (0–1) |
| `TAKE_PROFIT_RR` | `2.0` | Legacy single-TP RR ratio |
| `MAX_RISK_PER_TRADE_PCT` | `1.0%` | Base risk per trade |
| `DAILY_LOSS_CAP_PCT` | `3.0%` | Halt entries after this daily loss |
| `WEEKLY_LOSS_CAP_PCT` | `8.0%` | Halt entries after this weekly loss |
| `MAX_OPEN_POSITIONS` | `2` | Max simultaneous positions |
| `MAX_DAILY_TRADES` | `2` | Max new entries per calendar day |
| `MAX_POSITION_PCT_OF_BALANCE` | `10.0%` | Hard cap per trade |
| `LOOP_INTERVAL_SECONDS` | `60` | main.py loop cadence |
| `MARKET_CONTEXT_REFRESH_MIN` | `30` | Global market stats poll interval |
| `BTC_DOM_ALTCOIN_RESTRICT` | `55.0%` | Reduce altcoin entries above this |
| `MARKET_DROP_NO_TRADE_PCT` | `−3.0%` | No new entries if market drops this much |
| `SESSION_FILTER_ENABLED` | `True` | London/NY session gate |
| `LONDON_OPEN/CLOSE` | `07:00–12:00 UTC` | London session window |
| `NY_OPEN/CLOSE` | `13:00–17:00 UTC` | New York session window |
| `BREAKEVEN_R` | `1.0R` | Move SL to entry after this profit |
| `TRAIL_START_R` | `1.5R` | Start trailing at this profit |
| `ATR_TRAIL_MULT` | `1.0` | Trail distance = ATR × this |

**FALLBACK_PAIRS:** BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, AVAXUSDT, DOTUSDT, LINKUSDT

---

### 3.2 main.py

**Purpose:** Automatic trading mode (no Claude). Executes signals when strategy strength ≥ 0.80.  
**Run:** `python main.py`  
**Use when:** You want fully automated execution without Claude API costs.

**Key functions:**

| Function | Description |
|----------|-------------|
| `main()` | Entry point — validates config, creates all components, calls `run()` |
| `run(api, risk_mgr, coin_selector, market_ctx, ws_feed)` | Main 60-second loop |
| `_handle_entry(signal, balance, sym_info, api, risk_mgr)` | Sizes and executes market buy |
| `_handle_exit(symbol, position, exit_reason, price, api, risk_mgr)` | Executes partial/full exits |
| `_is_active_session()` | Returns True during London or NY session |
| `_validate_config()` | Checks MEXC credentials present |
| `_round_step(value, step, precision)` | Floor-rounds to exchange lot step |

**Loop sequence (every 60 seconds):**
1. Fetch active pairs from CoinSelector
2. Refresh USDT balance
3. Check all open positions for SL/TP/trailing exits
4. Daily loss cap check
5. Global market context gates (Coinranking /stats)
6. Session filter (London/NY only)
7. Kill zone check
8. Global market filter (BTC dom, crash, greed, bias)
9. Per-symbol: ATR → correlation → position limit → order book → 4H structure → signal → execute

**Signal threshold:** `signal.strength >= 0.80` (vs 0.65 in claude_trader.py — lower bar since Claude adds an extra gate)

---

### 3.3 claude_trader.py

**Purpose:** AI-supervised trading engine. Every qualifying signal is reviewed by Claude Sonnet before execution.  
**Run:** `python claude_trader.py`  
**Use when:** Deploying on Railway in production.

**Class:** `ClaudeTrader`

**Key constants:**
```
_LOOP_INTERVAL       = 300s  (5 minutes)
_SMART_EXIT_INTERVAL = 900s  (15 minutes)
_CLAUDE_MODEL        = "claude-sonnet-4-6"
_MAX_CLAUDE_CANDIDATES = 3   (top-N signals sent to Claude per cycle)
```

**Key methods:**

| Method | Description |
|--------|-------------|
| `run()` | Main 5-minute loop |
| `_evaluate_coin_signal(symbol, trade_type)` | Runs all pre-Claude filters for one symbol |
| `analyze_signal_with_claude(signal, ctx, positions_count, daily_pnl_pct, sentiment)` | Entry gate Claude call |
| `_execute_entry(signal)` | Sizes, executes buy, places OCO |
| `_handle_exits()` | Checks hard SL/TP/trailing exits |
| `_check_smart_exits()` | 15-minute intelligent exit analysis |
| `_smart_exit_one(symbol, position, btc, fg_val)` | Per-position smart exit decision tree |
| `_execute_smart_close(...)` | Full close via smart exit |
| `_execute_partial_smart_close(...)` | 50% close via smart exit |
| `_btc_market_check()` | Fetches BTCUSDT 1H data for exit context |
| `_calc_rsi(closes, period)` | Wilder's RSI implementation |
| `_rsi_divergence(df, lookback)` | Bearish divergence detection |
| `_volume_dried_up(df, threshold)` | Volume collapse detection |
| `_build_signal_prompt(...)` | Formats entry signal for Claude |
| `_build_exit_prompt(...)` | Formats position state for Claude exit review |
| `_log_decision(symbol, response, trade_type)` | Appends to claude_decisions.log |
| `_log_trade(signal, response, fill_price, quantity, order_id)` | Appends to trade-journal.json |
| `_maybe_send_daily_summary()` | Sends Telegram summary at 23:55–23:59 UTC |

**Two-phase signal collection:**
- **Phase 1:** Scan all `active_pairs` through pre-Claude filters → collect `qualifying` signals
- **Phase 2:** Sort by score (desc) → take top 3 → send each to Claude

**Claude entry system prompt rules:**
- Only approve trades with 8+/10 confidence
- BTC bearish structure → NO TRADE
- Fear & Greed > 75 → NO TRADE
- RR < 1:3 → NO TRADE
- Signal score < 8 → NO TRADE
- When in doubt → NO TRADE

**Smart exit decision tree (per position, every 15 min):**
1. BTC BEARISH structure → immediate close
2. BTC dropped ≥ 2% in 1 hour → immediate close
3. Fear & Greed < 25 (extreme fear) → immediate close
4. Symbol 1H structure flipped BEARISH (CHoCH) → immediate close
5. Price broke below entry zone (< entry × 0.997) → immediate close
6. At 1.5R and break-even not active → set break-even SL, refresh OCO
7. At 2.0R and break-even active, trailing not active → activate trailing stop
8. RSI bearish divergence detected → Claude evaluation
9. Volume dried up + structure non-bullish → Claude evaluation
10. Position open > 8 hours → Claude evaluation
11. BTC volume very low (< 0.5× avg) → Claude evaluation
12. Claude returns CLOSE/PARTIAL_CLOSE/MOVE_SL/HOLD → act accordingly

**Telegram command handler:** `TelegramCommandHandler` (runs as daemon thread)
- `/balance` — USDT free balance + total equity
- `/positions` — all open positions with live P&L
- `/status` — bot state, BTC dom, kill zone, last scan summary
- `/stop` — pause new entries (monitoring continues)
- `/start` — resume new entries
- `/report` — today's trade count, wins/losses, P&L
- `/pairs` — current active trading pairs list
- `/help` — command list

---

### 3.4 strategy.py

**Purpose:** Complete ICT/SMC signal generation engine.  
**~1,530 lines.** Pure analysis — no API calls, no side effects.

**Key data structures:**

```python
@dataclass
class TradeSignal:
    symbol:             str
    entry_price:        float
    stop_loss:          float
    take_profit:        float    # legacy single-TP
    tp1:                float    # 1:1 RR (33%)
    tp2:                float    # 2:1 RR (33%)
    tp3:                float    # 3:1 RR (34%)
    zone_type:          str      # "FVG" | "OB" | "FVG+OB" | "SWEEP+..."
    kill_zone:          str      # "LONDON" | "NEW_YORK" | ...
    structure:          str      # "BULLISH" | "BEARISH" | "RANGING"
    score:              int      # 0–10
    score_breakdown:    dict
    strength:           float    # 0.0–1.0
    reason:             str
    liquidity_sweep:    bool
    displacement:       bool
    fvg_present:        bool
    ob_present:         bool
    ote_zone:           bool     # price in OTE 62–79% retracement
    discount_zone:      bool     # price below 50% of range
    confirmation_candle: bool
    vwap_filter:        bool
    atr:                float
```

**Key functions:**

| Function | Description |
|----------|-------------|
| `candles_to_df(klines)` | Convert MEXC raw klines to pandas DataFrame |
| `detect_kill_zone()` | Returns active session name or None |
| `detect_market_structure(df)` | BOS/CHoCH analysis → BULLISH/BEARISH/RANGING |
| `generate_signal(symbol, df, htf_df, trade_type)` | Full ICT/SMC signal pipeline |
| `detect_fvg(df)` | Fair Value Gap detection (3-candle pattern) |
| `detect_order_blocks(df)` | Order Block detection (impulse + mitigation) |
| `detect_liquidity_sweep(df)` | Equal highs/lows swept + reversal |
| `detect_displacement(df)` | Strong momentum candle detection |
| `detect_ote_zone(df, swing_high, swing_low)` | OTE 62–79% retracement zone |
| `_calc_atr(df, period)` | ATR calculation |
| `_score_signal(...)` | 10-point ICT signal scoring |

**10-point signal scoring breakdown:**
| Component | Points | Condition |
|-----------|--------|-----------|
| Liquidity sweep | +2 | Equal highs/lows swept before reversal |
| Displacement | +2 | Strong momentum candle after sweep |
| FVG present | +1 | Valid fair value gap in signal direction |
| Order Block | +1 | Valid OB with impulse ≥ 0.5% |
| OTE zone | +1 | Price in 62–79% Fibonacci retracement |
| Discount zone | +1 | Price below 50% of range (buy at discount) |
| Kill zone bonus | +1 | Entry during London or NY session |
| HTF alignment | +1 | 4H structure is BULLISH |

**Score threshold:** ≥ 8/10 required in claude_trader.py (≥ 6/10 internal strategy minimum `_MIN_SCORE`)

**Kill zones:**
- `LONDON` — 07:00–10:00 UTC (+1 bonus)
- `NEW_YORK` — 13:00–16:00 UTC (+1 bonus)
- `LONDON_CLOSE` — 10:00–12:00 UTC (+1 bonus)
- `FRIDAY_REDUCED` — Friday during sessions (half position size)
- `OFF_HOURS` — Scans but no bonus
- `None` — Weekend (Saturday/Sunday) — no trading

---

### 3.5 risk_manager.py

**Purpose:** Position sizing, risk gates, partial TP state, OCO management, persistence.

**Position dataclass fields:**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | str | MEXC trading pair |
| `side` | str | Always "BUY" (spot long only) |
| `entry_price` | float | Signal entry price |
| `fill_price` | float | Actual average fill price |
| `quantity` | float | Total base asset purchased |
| `stop_loss` | float | Current SL price (updates with trail) |
| `take_profit` | float | Backward-compat alias for tp1 |
| `tp1/tp2/tp3` | float | Partial TP targets (1:1, 2:1, 3:1 RR) |
| `tp1_hit/tp2_hit` | bool | Partial TP flags |
| `break_even_active` | bool | SL moved to break-even after TP1 |
| `trailing_active` | bool | ATR trailing active after TP2 |
| `open_time` | str | ISO timestamp of entry |
| `score` | int | ICT signal score at entry |
| `entry_atr` | float | ATR at entry (used for trailing distance) |
| `oco_list_id` | str | Exchange OCO order list ID |
| `oco_tp_price` | float | OCO TP leg price |
| `oco_sl_price` | float | OCO SL leg price |

**Risk gates in `can_open_position(symbol)` (checked in order):**
1. Symbol not already open
2. `len(positions) < MAX_OPEN_POSITIONS` (2)
3. Daily loss < 3% of session start balance
4. Weekly loss < 8% of week start balance
5. `daily_trades < MAX_DAILY_TRADES` (2)
6. `consecutive_losses < 4`

**Position sizing logic:**
- Base risk = balance × 1.0%
- Win streak (3+): risk raised to 1.1% (max 1.5%)
- 2 consecutive losses: size × 0.75
- 3 consecutive losses: size × 0.50
- 4+ consecutive losses: return None (halt)
- Friday (FRIDAY_REDUCED kill zone): size × 0.50
- Hard cap: position cost ≤ balance × 10%
- Skip if notional < `min_notional` (typically $5)

**TP milestone transitions:**
- `handle_tp1_hit()` → sets `break_even_active=True`, moves SL to `entry × 1.001`, cancels OCO, places new OCO (TP2/BE-SL)
- `handle_tp2_hit()` → sets `trailing_active=True`, cancels OCO, places new OCO (TP3/current-SL)
- `update_trailing_stop()` → advances SL by `ATR × 1.0`, never backward

**OCO methods:**
- `place_oco_for_position(symbol, api, tp_price, sl_price, tick_size)` — places SELL OCO, saves `orderListId`, retries once after 3s
- `cancel_oco_for_position(symbol, api)` — cancels live OCO, always clears stored ID

---

### 3.6 mexc_api.py

**Purpose:** MEXC v3 REST API transport layer. HMAC-SHA256 signing, retries, error normalisation.

**Critical MEXC quirks (documented in code):**
1. **Signed params go in URL query string for ALL methods** (GET, POST, DELETE). Sending params in a form-encoded body returns error 700013 "Invalid content Type".
2. **Kline interval is "60m" not "1h"**. Using "1h" returns error -1121.
3. **Symbol status is "1" not "ENABLED"**. `_ACTIVE_STATUSES = {"ENABLED", "TRADING", "1", 1, True}`.
4. **Public endpoints must NOT have X-MEXC-APIKEY header**. The header triggers auth-check mode and returns 700004 if no signature is present.

**Class:** `MEXCSpotAPI`

| Method | Endpoint | Description |
|--------|----------|-------------|
| `get_server_time()` | GET /api/v3/time | Server time in ms |
| `get_exchange_info(symbol)` | GET /api/v3/exchangeInfo | Symbol filters and precision |
| `get_klines(symbol, interval, limit)` | GET /api/v3/klines | OHLCV candles |
| `get_ticker_price(symbol)` | GET /api/v3/ticker/price | Current best price |
| `get_order_book(symbol, limit)` | GET /api/v3/depth | Bids/asks |
| `get_24h_tickers()` | GET /api/v3/ticker/24hr | All symbols rolling stats |
| `get_account_info()` | GET /api/v3/account | Signed — account balances |
| `get_usdt_balance()` | → get_account_info | Free USDT balance |
| `get_asset_balance(coin)` | → get_account_info | Free balance for any asset |
| `place_market_buy(symbol, qty)` | POST /api/v3/order | Market buy |
| `place_market_sell(symbol, qty)` | POST /api/v3/order | Market sell |
| `place_limit_buy/sell(symbol, qty, price)` | POST /api/v3/order | Limit orders |
| `cancel_order(symbol, order_id)` | DELETE /api/v3/order | Cancel single order |
| `place_oco_order(symbol, side, qty, price, stop_price, stop_limit_price)` | POST /api/v3/order/oco | OCO bracket order |
| `cancel_oco_order(symbol, order_list_id)` | DELETE /api/v3/orderList | Cancel OCO pair |
| `get_oco_order(order_list_id)` | GET /api/v3/orderList | Query OCO status |
| `cancel_all_orders(symbol)` | DELETE /api/v3/openOrders | Cancel all open orders |
| `get_symbol_info(symbol)` | → get_exchange_info | Normalised precision + filters dict |
| `get_all_usdt_spot_symbols()` | → get_exchange_info | All active USDT spot pairs |

**Retry policy:** 3 attempts. 429 → sleep 60s. Timeout → sleep 5s. ConnectionError → exponential backoff (2, 4, 8s). `MEXCAPIError` propagated immediately.

**Error logging:** Dedicated file handler writes to `error.log` at WARNING level.

---

### 3.7 coin_selector.py

**Purpose:** Builds and maintains the dynamic halal-filtered, quality-scored trading universe.

**Class:** `CoinSelector`

**13-step refresh pipeline (runs every 4 hours):**

| Step | Operation |
|------|-----------|
| 1 | Fetch 200 coins from Coinranking ordered by 24h volume |
| 2 | Symbol + name haram quick-filter (HARAM_SYMBOLS + HARAM_NAME_KEYWORDS) |
| 3 | Drop `isWrappedTrustless` and `lowVolume` flagged coins |
| 4 | Numeric gates: mcap ≥ $500M, volume ≥ $50M, \|change\| ≤ 20% |
| 5 | MEXC cross-reference (only coins listed as active USDT spot) |
| 6 | Fetch `/coin/{uuid}` detail for top 40 candidates (tags, supply, exchange count) |
| 7a | Fetch 7-day price history for weekly trend check |
| 7b | Fetch markets for DEX ratio check; remove coins where DEX volume > 30% |
| 8 | Fetch top 30 exchanges to build reputable exchange reference set |
| 9 | Tag-based halal filter (stablecoin, wrapped, meme, lending tags) |
| 10 | Score each coin 0–10 using `_score_coin()` |
| 11 | Keep score ≥ MIN_COIN_SCORE (7); sort by score desc, then MEXC volume desc |
| 12 | Cap at MAX_SELECTED_PAIRS (50); supplement with FALLBACK_PAIRS if < 5 qualify |
| 13 | Save to `active_pairs.json` |

**10-point coin scoring (ScoreBreakdown):**
| Axis | Max | Condition |
|------|-----|-----------|
| Market cap | +2 | > $1B (+2), $500M–$1B (+1) |
| 24h volume | +2 | > $100M (+2), $50M–$100M (+1) |
| Exchange count | +1 | Listed on 10+ exchanges |
| Stability | +1 | \|24h change\| < 15% |
| Supply health | +1 | Circulating ≥ 90% of total supply |
| Narrative tags | +1 | L1/L2/DeFi/infra/gaming/metaverse tag |
| Coinranking tier | +1 | Tier = 1 (platform's own quality gate) |
| Sparkline swing | +1 | Intra-day swing < 20% |

**Halal filters:**
- **HARAM_TAGS:** stablecoin, wrapped, meme
- **HARAM_SYMBOLS:** 80+ exact ticker blocks (gambling, adult, alcohol, weapons, lending, stablecoins, wrapped tokens)
- **HARAM_NAME_KEYWORDS:** 20+ substring matches (lending protocol, interest bearing, etc.)
- **NARRATIVE_TAGS:** layer-1, layer-2, web3, dao, nft, dex, exchange, privacy, metaverse, gaming

**Public methods:**
- `get_pairs()` — returns cached pair list, triggers refresh if stale
- `get_quality_score(mexc_symbol)` — 0–10 score; returns 10.0 for unknown symbols (fallback pairs never blocked)
- `get_coin_metadata(mexc_symbol)` — full ScoredCoin record

---

### 3.8 coin_ranker.py

**Purpose:** Coinranking Professional API client. Stateless — instantiate once per refresh cycle.

**Class:** `CoinRankingClient`

**Auth:** `x-access-token` header with `COINRANKING_API_KEY` from config.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `get_coins_page(offset, limit, order_by)` | GET /coins | One page of coin list data |
| `get_all_coins(pages, limit)` | → get_coins_page × N | Paginated full list |
| `get_coin_detail(uuid)` | GET /coin/{uuid} | Tags, supply, exchange count, description |
| `get_coin_details_batch(uuids)` | → get_coin_detail × N | Batch detail fetch with 0.3s pause |
| `get_coin_history(uuid, time_period)` | GET /coin/{uuid}/history | Price history (7d default) |
| `get_coin_exchanges(uuid, limit)` | GET /coin/{uuid}/exchanges | Exchange listing |
| `get_top_exchanges(limit)` | GET /exchanges | Top exchanges by 24h volume |
| `get_stats()` | GET /stats | Global market stats (BTC dom, total mcap) |
| `get_coin_markets(uuid, limit)` | GET /coin/{uuid}/markets | Markets for DEX ratio |

**Rate limiting:** 429 → sleep `Retry-After` seconds (default 65s). 0.3–0.4s courtesy delays between batch calls.

---

### 3.9 filters.py

**Purpose:** Gate-keeper pre-trade filters. Side-effect-free; each returns a plain dict.

**Filter functions:**

| Function | Returns | Block condition |
|----------|---------|-----------------|
| `check_atr_filter(candles, price)` | `{tradeable, atr_pct, reason}` | ATR% > 3.0% (too choppy) or < 0.3% (dead market) |
| `check_fake_bos(candles, swing_high)` | `{real_bos, confidence, reason}` | Break < 0.2%, price fell back within 2 candles, or volume < avg |
| `check_correlation_guard(symbol, open_positions)` | `{allowed, reason}` | Second position in same correlation group |
| `check_order_book(symbol, entry_price, mexc_api)` | `{liquid_enough, spread_pct, reason}` | Spread > 0.15% or bid depth < $10,000 within 0.5% |
| `check_global_market(market_context, trade_type)` | `{tradeable, reason}` | Market down > 3%, BTC dom > threshold, greed > 75, BTC bearish |

**Correlation groups:**
- Group 1: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT — max 1 open
- Group 2: XRPUSDT, ADAUSDT, AVAXUSDT, DOTUSDT, MATICUSDT — max 1 open
- Group 3: everything else — no intra-group limit

**BTC dominance thresholds by trade type:**
- Swing: 60%
- Day trading: 65%
- Default (conservative): 58%

---

### 3.10 news_filter.py

**Purpose:** High-impact news guard. Three data sources cached and fail-open.

**Data sources:**
1. **ForexFactory** (`nfs.faireconomy.media/ff_calendar_thisweek.json`) — USD High-impact economic events. Cache TTL: 3600s.
2. **CoinDesk + Cointelegraph RSS** — Crypto news headlines. Cache TTL: 600s.
3. **Fear & Greed Index** (`api.alternative.me/fng`) — Market sentiment 0–100. Cache TTL: 1800s.

**Block triggers (±30 minutes window):**
- ForexFactory: FOMC, CPI, NFP, PCE, GDP, Interest Rate Decision
- RSS keywords: "interest rate", "fed rate", "fomc", "cpi", "sec ban/rejects/approves", "crypto ban", "exchange hack/crash/collapse", "flash crash", "etf rejected/approved", "massive liquidation"

**Public functions:**

| Function | Returns | Description |
|----------|---------|-------------|
| `is_news_time()` | `{block, reason, event, time}` | True if high-impact event within ±30 min |
| `get_crypto_sentiment()` | `{sentiment, score, top_news, fear_greed}` | Blended sentiment score |

**Sentiment scoring:**
- Fear & Greed base: `(value - 50) / 50` → [-1.0, +1.0]
- RSS keyword polarity: bullish/bearish keywords in top 20 headlines
- Blend: 60% F&G + 40% RSS polarity
- Output: BULLISH (≥ +0.2), BEARISH (≤ −0.2), NEUTRAL

---

### 3.11 telegram_alerts.py

**Purpose:** Fire-and-forget Telegram notifications. Never propagates errors to caller.

**Required env vars:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

**Alert functions:**

| Function | Trigger | Message format |
|----------|---------|----------------|
| `trade_opened(...)` | New position opened | 🟢 TRADE OPENED — entry, SL, TP1/2/3, RR, session |
| `trade_closed(...)` | Position fully closed | ✅ WIN / ❌ LOSS — symbol, PnL, exit price, balance |
| `tp_hit(symbol, tp_level, pnl, price)` | Partial TP hit | 🎯 TP{N} HIT — partial PnL, price |
| `sl_hit(symbol, loss_usdt, loss_pct, price)` | Stop loss triggered | 🔴 STOP LOSS HIT — loss amount |
| `daily_summary(trades, wins, losses, pnl, balance)` | At 23:55 UTC daily | 📅 DAILY SUMMARY — stats, win rate |
| `no_trade(symbol, reason, score)` | Claude rejects qualified signal | ⏸ NO TRADE — reason |
| `system_error(error_message)` | Unhandled exception in main loop | ⚠️ SYSTEM ERROR |
| `market_filter(reason, btc_dom)` | Macro gate blocks all entries | 🔴 MARKET FILTER — BTC dom, reason |
| `smart_exit(symbol, reason, action, pnl, pct)` | Smart exit system close/partial | ⚠️ SMART EXIT — reason, action, P&L |
| `position_update(symbol, decision, reason, pnl, pct)` | Claude HOLD/MOVE_SL/PARTIAL | 📊/⚠️/🛡️ POSITION UPDATE |
| `news_block(event, reason, block_min)` | News filter active | ⚠️ NEWS FILTER ACTIVE |

**Class:** `TelegramCommandHandler`  
- Runs as a daemon thread using Telegram long-polling (8s server-side timeout)
- Security: silently drops messages from any chat ID other than `TELEGRAM_CHAT_ID`
- 8 commands: `/balance /positions /status /stop /start /report /pairs /help`
- `trading_enabled` bool — read by main loop to gate new entries

---

### 3.12 market_context.py

**Purpose:** Polls Coinranking `/v2/stats` every 30 minutes in a background daemon thread. Saves snapshot to `market_context.json`.

**Class:** `MarketContextPoller`

**MarketContext dataclass:**
```python
@dataclass
class MarketContext:
    fetched_at:         str    # ISO timestamp
    total_market_cap:   float  # USD
    total_24h_volume:   float  # USD
    btc_dominance:      float  # %, e.g. 54.7
    total_coins:        int
    total_exchanges:    int
    altcoin_restricted: bool   # True when btc_dominance > 55%
    prev_market_cap:    float  # previous snapshot (for change calculation)
    market_change_pct:  float  # approx 24h % change
```

**Gate methods:**
- `is_safe_to_trade()` — False when market_change_pct < −3.0%
- `is_altcoin_restricted()` — True when btc_dominance > 55%
- `get_context()` — returns latest MarketContext (thread-safe)

**Note:** `market_change_pct` is approximated from consecutive mcap snapshots (no direct 24h change from /stats endpoint).

---

### 3.13 ws_price_feed.py

**Purpose:** Real-time price feed from Coinranking WebSocket streams. Two concurrent connections per daemon thread.

**Class:** `WSPriceFeed`

**Streams:**
- `/rates` — price updates per coin UUID (1s throttle)
- `/tickers` — OHLCV + DEX/CEX volume split per coin UUID

**Key methods:**
- `get_price(mexc_symbol)` — latest real-time price (None if not subscribed)
- `get_ticker(mexc_symbol)` — TickerData with volume breakdown
- `get_dex_ratio(mexc_symbol)` — DEX volume fraction 0.0–1.0
- `is_dex_spike(mexc_symbol, threshold=0.30)` — True if DEX > 30% of volume
- `update_uuid_map(uuid_map)` — hot-swap UUID map after coin refresh
- `start()` / `stop()` — lifecycle management

**TickerData dataclass:** `uuid, price, base_volume, quote_volume, dex_volume, cex_volume, updated_at`

**Reconnection:** Auto-reconnects after 10 seconds on any disconnect.

**Dependency:** `pip install websockets`

---

## 4. Full System Flow

### Step 1 — Startup
`ClaudeTrader.__init__()`:
- Validates `ANTHROPIC_API_KEY`
- Creates `MEXCSpotAPI`, `RiskManager` (loads `positions.json`), `CoinSelector`, `TelegramCommandHandler`

`ClaudeTrader.run()`:
- Validates MEXC credentials
- Fetches opening USDT balance → `set_session_balance(balance)`
- Builds initial pair list via `CoinSelector.get_pairs()` (triggers 13-step pipeline)
- Starts `MarketContextPoller` daemon thread
- Starts `WSPriceFeed` daemon thread (Coinranking WS)
- Starts `TelegramCommandHandler` daemon thread → sends "🤖 Bot started and ready!"

### Step 2 — Every 5 Minutes: Refresh
- Fetch updated active pairs (CoinSelector returns cache if < 4h old)
- Update `WSPriceFeed` UUID map if pairs changed
- Refresh USDT balance from MEXC

### Step 3 — Hard Exit Check (every cycle)
`_handle_exits()`:
- For each open position: get price (WS → REST fallback)
- If `trailing_active`: advance trailing SL via `update_trailing_stop()`
- `check_exit()`: SL → TP1 → TP2 → TP3 → TAKE_PROFIT
- On TP1/TP2: cancel OCO → market sell partial → `handle_tp1_hit/tp2_hit()` → place new OCO
- On SL/TP3/TAKE_PROFIT: cancel OCO → market sell remaining → record PnL → remove position → Telegram alert

### Step 4 — Smart Exit Check (every 15 minutes)
`_check_smart_exits()`:
- Fetch BTC 1H context once (bias, 1h change, volume ratio)
- Fetch Fear & Greed index once
- For each open position: run `_smart_exit_one()` decision tree (see §3.3 above)

### Step 5 — Entry Gates
Gate 1: Kill zone check — weekend blocks entirely  
Gate 2: Global market filter (`check_global_market`) — market crash, BTC dom, F&G  
Gate 2b: News filter (`is_news_time`) — economic events ±30 min  
Gate 3: `/stop` command pause check  
Gate 4: Daily loss cap (3%) and global position limits (2 max, 2 daily)

### Step 6 — Per-Symbol Pre-Claude Filter Chain
For each pair in `active_pairs` (up to MAX_SELECTED_PAIRS):
1. Coin quality score ≥ `MIN_COIN_SCORE` (7)
2. DEX spike check (< 30% DEX volume)
3. `check_atr_filter()` — 0.3%–3.0% ATR range
4. `check_correlation_guard()` — max 1 per correlation group
5. `can_open_position(symbol)` — all risk gates
6. `check_order_book()` — spread ≤ 0.15%, depth ≥ $10,000
7. 4H structure: BEARISH blocks always; NEUTRAL allowed for daytrading
8. `generate_signal()` — ICT/SMC scoring
9. Signal score ≥ 8/10
10. Signal strength ≥ 0.65
11. Active FVG or OB zone required
12. Price within 1% of zone entry

### Step 7 — Two-Phase Signal Collection
- **Phase 1:** Collect all passing signals into `qualifying` list
- **Phase 2:** Sort by score (descending) → take top 3 → send to Claude

### Step 8 — Claude Entry Review
`analyze_signal_with_claude()`:
- Sends signal data + market context + news sentiment in structured prompt
- Parses JSON response: `{decision, confidence, reason, risk_level, invalidation}`
- Execute only if `decision == "BUY"` AND `confidence >= 8`
- On rejection: `tg.no_trade()` Telegram alert

### Step 9 — Entry Execution
`_execute_entry(signal)`:
1. Fetch fresh balance from MEXC
2. `calculate_quantity()` — size based on 1% risk, ATR-based stop distance
3. `place_market_buy()` — returns fill price from `cummulativeQuoteQty / executedQty`
4. Enforce 3% hard SL cap from fill price
5. Recompute TPs from actual fill if slippage > 0.1%
6. `add_position()` — saves to positions.json
7. `place_oco_for_position()` — SELL OCO at TP1 and SL on exchange
8. `tg.trade_opened()` Telegram alert
9. Append to `trade-journal.json`

### Step 10 — Position Lifecycle Until Exit
- Hard SL/TP exits handled every 5 minutes by `_handle_exits()`
- Smart exits evaluated every 15 minutes by `_check_smart_exits()`
- Trailing stop advanced each cycle when `trailing_active == True`
- Break-even SL set automatically at TP1 hit (entry + 0.1%)
- OCO updated at each TP milestone (TP1 → place OCO for TP2/BE; TP2 → place OCO for TP3/trail-SL)
- Daily summary sent at 23:55–23:59 UTC

---

## 5. Entry Gate Hierarchy

```
[Weekend?] ─────────────────────────────────────────────────→ SKIP (Sat/Sun)
[Market crash > 3%?] ───────────────────────────────────────→ SKIP
[BTC dominance > 58%?] ─────────────────────────────────────→ SKIP (or reduce)
[News event ±30 min?] ──────────────────────────────────────→ SKIP
[/stop command active?] ────────────────────────────────────→ SKIP
[Daily loss ≥ 3%?] ─────────────────────────────────────────→ SKIP
[Positions ≥ 2?] ───────────────────────────────────────────→ SKIP
[Daily trades ≥ 2?] ────────────────────────────────────────→ SKIP
[Coin quality < 7?] ────────────────────────────────────────→ SKIP
[DEX spike > 30%?] ─────────────────────────────────────────→ SKIP
[ATR < 0.3% or > 3%?] ─────────────────────────────────────→ SKIP
[Same correlation group open?] ─────────────────────────────→ SKIP
[Order book spread > 0.15%?] ───────────────────────────────→ SKIP
[Order book depth < $10,000?] ──────────────────────────────→ SKIP
[4H structure BEARISH?] ────────────────────────────────────→ SKIP
[Signal score < 8?] ────────────────────────────────────────→ SKIP
[Signal strength < 0.65?] ──────────────────────────────────→ SKIP
[No FVG or OB?] ────────────────────────────────────────────→ SKIP
[Price > 1% from zone?] ────────────────────────────────────→ SKIP
[Claude confidence < 8?] ───────────────────────────────────→ SKIP
[Claude decision ≠ BUY?] ───────────────────────────────────→ SKIP
                                                            ↓
                                                       EXECUTE BUY
```

---

## 6. Exit System

### Hard Exits (every 5 min, `_handle_exits()`)
Checked in priority order:
1. `price ≤ stop_loss` → STOP_LOSS: full close of remaining qty
2. `price ≥ tp1` (and not hit) → TP1: sell 33%, set break-even, refresh OCO
3. `price ≥ tp2` (after TP1) → TP2: sell 33%, activate trailing, refresh OCO
4. `price ≥ tp3` (after TP2) → TP3: sell remaining 34%

### Smart Exits (every 15 min, `_check_smart_exits()`)
Immediate close triggers (no Claude):
- BTC 1H structure BEARISH
- BTC 1H candle drop ≥ 2%
- Fear & Greed < 25
- Symbol 1H structure flipped BEARISH
- Price < entry × 0.997

Claude evaluation triggers:
- RSI bearish divergence (price up, RSI down > 3 points over 5 bars)
- Volume dry-up (< 50% of 20-bar avg) + non-bullish structure
- Position open > 8 hours
- BTC volume < 50% of 20-bar avg

Profit protection (no close):
- At 1.5R: set break-even SL (entry × 1.001)
- At 2.0R: activate trailing stop

### Position Sizing at Exit
- TP1: `partial_qty(1)` = qty × 0.33
- TP2: `partial_qty(2)` = qty × 0.33
- TP3 / SL: `remaining_qty()` = qty − TP1_sold − TP2_sold

---

## 7. OCO Order Lifecycle

```
ENTRY EXECUTED
     │
     ├─ place_oco_for_position(symbol, tp1, safe_sl)
     │    → MEXC OCO: SELL LIMIT @ tp1 + SELL STOP-LIMIT @ safe_sl
     │    → Position.oco_list_id saved to positions.json
     │
     ├─ TP1 HIT (software check)
     │    ├─ cancel_oco_for_position()        ← prevents double-execution
     │    ├─ place_market_sell(qty × 0.33)
     │    └─ handle_tp1_hit() → place_oco_for_position(tp2, be_sl)
     │
     ├─ TP2 HIT (software check)
     │    ├─ cancel_oco_for_position()
     │    ├─ place_market_sell(qty × 0.33)
     │    └─ handle_tp2_hit() → place_oco_for_position(tp3, trail_sl)
     │
     ├─ SMART EXIT: break-even activated
     │    ├─ cancel_oco_for_position()
     │    └─ place_oco_for_position(next_tp, be_sl)
     │
     └─ FULL CLOSE (SL/TP3/smart/Claude)
          └─ cancel_oco_for_position()        ← always cancel before sell
               → MEXC OCO cancelled
               → position removed from positions.json
```

**Crash protection:** OCO order on MEXC exchange side means positions are protected even if the bot goes offline. MEXC will execute the TP or SL leg automatically regardless of bot state.

---

## 8. All Thresholds and Limits

### Risk Limits
| Limit | Value | File |
|-------|-------|------|
| Risk per trade (base) | 1.0% of balance | config.py |
| Risk per trade (win streak ≥3) | up to 1.5% | risk_manager.py |
| Daily loss cap | 3.0% | config.py |
| Weekly loss cap | 8.0% | config.py |
| Max open positions | 2 | config.py |
| Max daily trades | 2 | config.py |
| Max position size | 10% of balance | config.py |
| Hard SL cap from fill | 3.0% | claude_trader.py, main.py |
| Friday position size | 50% reduction | risk_manager.py |
| 2 consecutive losses | 25% size reduction | risk_manager.py |
| 3 consecutive losses | 50% size reduction | risk_manager.py |
| 4 consecutive losses | No new trades | risk_manager.py |
| Break-even fee buffer | 0.1% above entry | risk_manager.py |
| Trailing ATR multiplier | 1.0× | risk_manager.py |

### Strategy Thresholds
| Threshold | Value | File |
|-----------|-------|------|
| Min signal score (claude_trader) | 8/10 | claude_trader.py |
| Min signal score (strategy internal) | 6/10 | strategy.py |
| Min signal strength (claude_trader) | 0.65 | claude_trader.py |
| Min signal strength (main.py) | 0.80 | main.py |
| Min FVG size | 0.10% | config.py |
| Min OB impulse | 0.50% | config.py |
| OB lookback candles | 30 | config.py |
| ATR SL multiplier | 1.5× | config.py |
| Max price distance from zone | 1.0% | claude_trader.py |
| Claude confidence minimum | 8/10 | claude_trader.py |

### Market Context Thresholds
| Threshold | Value | File |
|-----------|-------|------|
| Global market drop halt | −3.0% | config.py |
| BTC dominance altcoin restrict | 55.0% | config.py |
| BTC dom default gate | 58.0% | filters.py |
| BTC dom swing trade gate | 60.0% | filters.py |
| BTC dom day trade gate | 65.0% | filters.py |
| Fear & Greed extreme greed | > 75 | filters.py |
| F&G extreme fear (smart exit) | < 25 | claude_trader.py |

### Filter Thresholds
| Threshold | Value | File |
|-----------|-------|------|
| ATR max (too choppy) | 3.0% | filters.py |
| ATR min (dead market) | 0.3% | filters.py |
| Order book spread max | 0.15% | filters.py |
| Order book bid depth min | $10,000 | filters.py |
| DEX volume ratio max | 30% | coin_selector.py, ws_price_feed.py |
| BOS break magnitude min | 0.2% | filters.py |

### Coin Quality Gates
| Threshold | Value | File |
|-----------|-------|------|
| Minimum coin score | 7/10 | config.py |
| Minimum market cap | $500M | config.py |
| Minimum MEXC 24h volume | $50M | config.py |
| Maximum 24h price change | ±20% | config.py |

### Timing
| Timer | Value | File |
|-------|-------|------|
| Main loop interval (main.py) | 60s | config.py |
| Main loop interval (claude_trader.py) | 300s (5 min) | claude_trader.py |
| Smart exit interval | 900s (15 min) | claude_trader.py |
| Coin selector refresh | 4 hours | config.py |
| Market context refresh | 30 min | config.py |
| News block window | ±30 min | news_filter.py |
| Economic calendar cache TTL | 3600s | news_filter.py |
| RSS cache TTL | 600s | news_filter.py |
| Fear & Greed cache TTL | 1800s | news_filter.py |

---

## 9. Persistent State Files

| File | Written by | Purpose |
|------|------------|---------|
| `positions.json` | risk_manager.py | All open positions + counters (consecutive losses, daily trades) |
| `active_pairs.json` | coin_selector.py | Current trading universe with scores and metadata |
| `market_context.json` | market_context.py | Latest global market stats snapshot |
| `trade-journal.json` | claude_trader.py | Full record of every executed trade with Claude decisions |
| `.env` | User | API credentials and Telegram config |

---

## 10. Log Files

| File | Module | Content |
|------|--------|---------|
| `trading_bot.log` | main.py | Main loop events, entries, exits, errors |
| `claude_trader.log` | claude_trader.py | All events including [Diag] filter decisions |
| `claude_decisions.log` | claude_trader.py | One line per Claude verdict (timestamp, symbol, decision, confidence, reason) |
| `error.log` | mexc_api.py | MEXC API errors (HTTP errors, retries, max retries exceeded) |

---

## 11. Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MEXC_API_KEY` | Yes | MEXC API key (from MEXC account settings) |
| `MEXC_SECRET` | Yes | MEXC API secret (**must be `MEXC_SECRET`**, not `MEXC_API_SECRET`) |
| `ANTHROPIC_API_KEY` | Yes (claude_trader.py) | Claude API key |
| `COINRANKING_API_KEY` | No | Coinranking Professional key (has default hardcoded in config.py) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | No | Telegram chat ID to send alerts to |
| `CLAUDE_MODEL` | No | Override Claude model (default: `claude-sonnet-4-6`) |

---

## 12. Deployment Modes

### Local Development
```bash
python claude_trader.py   # AI-supervised mode (recommended)
python main.py            # Automatic mode (no Claude)
```

### Railway (Cloud)
- Set all environment variables in Railway dashboard
- Use `python claude_trader.py` as start command
- Both modes share `positions.json` — do NOT run both simultaneously

### Switching Modes
If Claude API is unavailable: signals are SKIPPED by claude_trader.py (conservative default).  
Switch to main.py to execute signals automatically without Claude review.

### Key Files at Runtime
- `positions.json` — must exist and be writable for restart recovery to work
- `.env` — loaded by config.py on startup; Railway env vars override this
- `active_pairs.json` — rebuilt every 4 hours; safe to delete (will be recreated)
- `market_context.json` — rebuilt every 30 min; safe to delete

---

*End of documentation.*
