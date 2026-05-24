/**
 * MEXC Spot Trading — MCP Server (Node.js, direct REST API v3)
 * =============================================================
 * Tools:
 *   get_balance       — USDT free / locked / total                [private]
 *   get_active_pairs  — halal top-20 from CoinGecko × MEXC spot   [public]
 *   evaluate_signal   — SMC (FVG + Order Block) check for any pair [public]
 *   execute_trade     — spot MARKET order                          [private]
 *   get_open_orders   — list open orders                           [private]
 *   cancel_order      — cancel an order by ID                      [private]
 *
 * Pair selection pipeline (refreshes every 4 h)
 * -----------------------------------------------
 *   1. Fetch top 500 coins from CoinGecko (2 pages × 250, sorted by 24h volume)
 *   2. Drop haram coins (gambling, adult, alcohol, weapons, riba/lending)
 *   3. Keep only coins with an active MEXC USDT spot pair
 *   4. Re-rank survivors by MEXC 24h USDT volume
 *   5. Apply $500k volume floor
 *   6. Cap at 20 pairs; supplement with fallbacks if < 5 qualify
 *
 * Signing rules (MEXC v3, verified)
 * ----------------------------------
 *   Public  GET  → no X-MEXC-APIKEY header, no signature
 *   Private GET  → X-MEXC-APIKEY header, signature in query string
 *   Private POST → X-MEXC-APIKEY header, signature in form-encoded BODY
 *   Private DEL  → X-MEXC-APIKEY header, signature in query string
 *
 * Requires Node.js >= 18 (global fetch).
 */

import { Server }               from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import crypto            from "node:crypto";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import dotenv             from "dotenv";

// ── Environment ───────────────────────────────────────────────────────────────
const __filename = fileURLToPath(import.meta.url);
const __dirname  = dirname(__filename);
dotenv.config({ path: join(__dirname, ".env") });

const API_KEY    = process.env.MEXC_API_KEY ?? "";
const API_SECRET = process.env.MEXC_SECRET  ?? "";
const BASE_URL   = "https://api.mexc.com";
const CG_BASE    = "https://api.coingecko.com/api/v3";
const RECV_WIN   = 5000;

// ── SMC constants (mirrors strategy.py) ──────────────────────────────────────
const ATR_PERIOD     = 14;
const ATR_SL_MULT    = 1.5;
const TP_RR          = 2.0;
const FVG_MIN_PCT    = 0.002;
const OB_MIN_IMPULSE = 0.005;
const OB_LOOKBACK    = 20;
const SCAN_CANDLES   = 100;

// ── Coin selector constants ───────────────────────────────────────────────────
const SELECTOR_TTL_MS    = 4 * 60 * 60 * 1000;   // 4 hours
const PAIRS_TTL_MS       = 5 * 60 * 1000;          // 5 min (MEXC active pairs)
const CG_PAGES           = 2;                       // 2 × 250 = 500 coins
const CG_PAGE_DELAY_MS   = 2500;                    // CoinGecko free-tier rate limit
const CG_TIMEOUT_MS      = 20_000;
const MIN_MEXC_VOLUME    = 500_000;                 // $500k USDT 24h floor
const MAX_SELECTED_PAIRS = 20;
const MIN_SELECTED_PAIRS = 5;

const FALLBACK_PAIRS = [
  "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
  "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
];

// ─────────────────────────────────────────────────────────────────────────────
//  Haram filter (ported from coin_selector.py)
// ─────────────────────────────────────────────────────────────────────────────

const HARAM_SYMBOLS = new Set([
  // Gambling / Betting / Lottery
  "BET", "DICE", "WINK", "WIN", "ROLL", "LOTTO", "JACKPOT", "LUCKY",
  "SPIN", "POKER", "BACCARAT", "SLOT", "CASINO", "ROULETTE", "SHUFFLE", "CSGOROLL",
  // Adult content
  "XXX", "PORN", "NSFW", "ADULT", "SEXC",
  // Alcohol
  "WINE", "BEER", "BREW", "WHISKY", "VODKA", "RUM", "GIN", "SAKE",
  // Weapons / Violence
  "GUN", "RIFLE", "BULLET", "BOMB", "AMMO",
  // Interest / Riba — lending & yield protocols
  "AAVE", "LEND", "COMP", "MKR", "DAI", "NEXO", "CEL", "XVS",
  "CREAM", "ALPACA", "EULER", "RDNT", "QI", "STRIKE", "IRON",
  "SILO", "MORPHO", "FLUID", "EXACTLY", "ANGLE", "NOTIONAL",
  "TERM", "MAPLE", "GOLDFINCH", "CLEARPOOL", "TRUEFI",
  "ONDO", "PENDLE",
]);

const HARAM_NAME_KEYWORDS = [
  // Gambling
  "gambling", "casino", " betting", "lottery", "lotto", "jackpot",
  "slot machine", "poker room", "baccarat", "roulette",
  // Adult
  "adult content", "pornograph", "erotic", " xxx ",
  // Alcohol
  "alcohol", " brewery", "distillery",
  // Weapons
  "firearms", "ammunition", "arms dealer",
  // Interest / lending
  "lending protocol", "borrowing protocol", "interest bearing",
  "interest-bearing", "fixed-rate lending", "undercollateral", "leveraged yield",
];

function isHalal(cgSymbol, cgName) {
  if (HARAM_SYMBOLS.has(cgSymbol.toUpperCase())) return false;
  const lower = cgName.toLowerCase();
  return !HARAM_NAME_KEYWORDS.some(kw => lower.includes(kw));
}

// ─────────────────────────────────────────────────────────────────────────────
//  Utility
// ─────────────────────────────────────────────────────────────────────────────

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ─────────────────────────────────────────────────────────────────────────────
//  HTTP layer
// ─────────────────────────────────────────────────────────────────────────────

async function jsonGet(url, params = {}, options = {}) {
  const qs      = new URLSearchParams(params).toString();
  const fullUrl = qs ? `${url}?${qs}` : url;
  const signal  = options.timeoutMs
    ? AbortSignal.timeout(options.timeoutMs)
    : undefined;
  const resp = await fetch(fullUrl, {
    headers: { Accept: "application/json", ...options.headers },
    signal,
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(`HTTP ${body.code ?? resp.status}: ${body.msg ?? resp.statusText}`);
  }
  return body;
}

function sign(queryString) {
  return crypto.createHmac("sha256", API_SECRET).update(queryString).digest("hex");
}

function stamp(params) {
  const p      = { ...params };
  p.timestamp  = Date.now();
  p.recvWindow = RECV_WIN;
  const qs     = new URLSearchParams(p).toString();
  p.signature  = sign(qs);
  return p;
}

/** Public MEXC GET — no auth, no signature. */
async function publicGet(endpoint, params = {}) {
  return jsonGet(`${BASE_URL}${endpoint}`, params);
}

/** Private MEXC GET — API key header, signature in query string. */
async function privateGet(endpoint, params = {}) {
  const qs = new URLSearchParams(stamp(params)).toString();
  return jsonGet(`${BASE_URL}${endpoint}?${qs}`, {}, {
    headers: { "X-MEXC-APIKEY": API_KEY },
  });
}

/** Private MEXC POST — API key header, signature in form body. */
async function privatePost(endpoint, params = {}) {
  const body = new URLSearchParams(stamp(params)).toString();
  const resp = await fetch(`${BASE_URL}${endpoint}`, {
    method:  "POST",
    headers: { "X-MEXC-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(`MEXC ${data.code ?? resp.status}: ${data.msg ?? resp.statusText}`);
  return data;
}

/** Private MEXC DELETE — API key header, signature in query string. */
async function privateDelete(endpoint, params = {}) {
  const qs = new URLSearchParams(stamp(params)).toString();
  const resp = await fetch(`${BASE_URL}${endpoint}?${qs}`, {
    method:  "DELETE",
    headers: { "X-MEXC-APIKEY": API_KEY },
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(`MEXC ${data.code ?? resp.status}: ${data.msg ?? resp.statusText}`);
  return data;
}

// ─────────────────────────────────────────────────────────────────────────────
//  Kline parser
// ─────────────────────────────────────────────────────────────────────────────

// MEXC v3: exactly 8 fields — [open_time, open, high, low, close, volume, close_time, quote_volume]
function parseKlines(raw) {
  return raw.map(r => ({
    openTime:    Number(r[0]),
    open:        parseFloat(r[1]),
    high:        parseFloat(r[2]),
    low:         parseFloat(r[3]),
    close:       parseFloat(r[4]),
    volume:      parseFloat(r[5]),
    closeTime:   Number(r[6]),
    quoteVolume: parseFloat(r[7]),
  }));
}

// ─────────────────────────────────────────────────────────────────────────────
//  SMC strategy engine (mirrors strategy.py)
// ─────────────────────────────────────────────────────────────────────────────

function calcATR(candles, period = ATR_PERIOD) {
  if (candles.length < 2) return 0;
  const alpha = 2 / (period + 1);
  let ema = 0;
  for (let i = 1; i < candles.length; i++) {
    const tr = Math.max(
      candles[i].high - candles[i].low,
      Math.abs(candles[i].high - candles[i - 1].close),
      Math.abs(candles[i].low  - candles[i - 1].close),
    );
    ema = i === 1 ? tr : tr * alpha + ema * (1 - alpha);
  }
  return ema;
}

function detectFVGs(candles) {
  const fvgs = [];
  const n = candles.length;
  for (let i = 2; i < n; i++) {
    const c0 = candles[i - 2], c2 = candles[i];
    if (c2.low > c0.high) {
      const gap = c2.low - c0.high;
      if (gap / ((c2.low + c0.high) / 2) >= FVG_MIN_PCT)
        fvgs.push({ type: "bullish", top: c2.low, bottom: c0.high, formedAt: i, filled: false });
    } else if (c2.high < c0.low) {
      const gap = c0.low - c2.high;
      if (gap / ((c0.low + c2.high) / 2) >= FVG_MIN_PCT)
        fvgs.push({ type: "bearish", top: c0.low, bottom: c2.high, formedAt: i, filled: false });
    }
  }
  for (const fvg of fvgs) {
    for (let j = fvg.formedAt + 1; j < n; j++) {
      if (fvg.type === "bullish" && candles[j].low  <= fvg.bottom) { fvg.filled = true; break; }
      if (fvg.type === "bearish" && candles[j].high >= fvg.top)    { fvg.filled = true; break; }
    }
  }
  return fvgs;
}

function detectOrderBlocks(candles) {
  const obs = [], n = candles.length, lookahead = 3;
  for (let i = 1; i < n - lookahead; i++) {
    const c = candles[i];
    if (c.close < c.open) {
      const impulse = (candles[i + lookahead].close - c.close) / c.close;
      if (impulse >= OB_MIN_IMPULSE) {
        const clean = Array.from({ length: lookahead - 1 }, (_, k) => candles[i + 1 + k])
          .every(x => x.close >= x.open);
        if (clean)
          obs.push({ type: "bullish", top: Math.max(c.open, c.close), bottom: c.low, formedAt: i, mitigated: false });
      }
    }
  }
  for (const ob of obs) {
    for (let j = ob.formedAt + lookahead + 1; j < n; j++) {
      if (ob.type === "bullish" && candles[j].low <= ob.bottom) { ob.mitigated = true; break; }
    }
  }
  return obs;
}

function recency(formedAt, total) {
  return Math.max(0, 1 - (total - formedAt) / Math.max(total, 1));
}

function generateSignal(symbol, candles) {
  if (candles.length < Math.max(ATR_PERIOD + 5, OB_LOOKBACK + 5)) return null;
  const n = candles.length, price = candles[n - 1].close, atr = calcATR(candles);
  const window = Math.min(n, OB_LOOKBACK * 2), slice = candles.slice(-window);
  const bullFVG = detectFVGs(slice).filter(f => f.type === "bullish" && !f.filled);
  const bullOB  = detectOrderBlocks(slice).filter(o => o.type === "bullish" && !o.mitigated);
  let best = null, bestStrength = 0;

  const makeSignal = (bottom, zoneType, strength) => {
    const sl = Math.max(bottom - atr * ATR_SL_MULT, 0);
    return { symbol, side: "BUY", zoneType, strength, entryPrice: price,
             stopLoss: sl, takeProfit: price + (price - sl) * TP_RR };
  };

  for (const fvg of bullFVG) {
    if (fvg.bottom <= price && price <= fvg.top) {
      const s = recency(fvg.formedAt, window);
      if (s > bestStrength) { bestStrength = s; best = makeSignal(fvg.bottom, "FVG", s); }
    }
  }
  for (const ob of bullOB) {
    if (ob.bottom <= price && price <= ob.top) {
      let s = Math.min(recency(ob.formedAt, window) + 0.15, 1.0);
      const hasConfluence = bullFVG.some(f => !f.filled && !(f.top < ob.bottom || f.bottom > ob.top));
      if (hasConfluence) s = Math.min(s + 0.20, 1.0);
      const zone = hasConfluence ? "FVG+OB" : "OB";
      if (s > bestStrength) { bestStrength = s; best = makeSignal(ob.bottom, zone, s); }
    }
  }
  return best;
}

// ─────────────────────────────────────────────────────────────────────────────
//  Coin selector  (ported from coin_selector.py)
// ─────────────────────────────────────────────────────────────────────────────

// Shape: Array<{ mexcSymbol, cgSymbol, name, volume24hUsd, priceChangePct24h }>
let _selectedPairs  = null;
let _selectorExpiry = 0;
let _refreshPromise = null;   // deduplicates concurrent refresh calls

// Inner cache for raw MEXC active pairs (shorter TTL)
let _mexcPairsSet    = null;
let _mexcPairsExpiry = 0;

async function fetchMexcActivePairs() {
  if (_mexcPairsSet && Date.now() < _mexcPairsExpiry) return _mexcPairsSet;
  const info    = await publicGet("/api/v3/exchangeInfo");
  const symbols = Array.isArray(info) ? info
                : Array.isArray(info.symbols) ? info.symbols : [];
  // MEXC v3 uses numeric status "1" = active, "2" = disabled.
  // Also accept isSpotTradingAllowed as a fallback guard.
  _mexcPairsSet    = new Set(
    symbols
      .filter(s => s.quoteAsset === "USDT" && String(s.status) === "1" && s.isSpotTradingAllowed !== false)
      .map(s => s.symbol)
  );
  _mexcPairsExpiry = Date.now() + PAIRS_TTL_MS;
  process.stderr.write(`[MEXC] exchangeInfo: ${symbols.length} total symbols, ${_mexcPairsSet.size} active USDT spot pairs\n`);
  return _mexcPairsSet;
}

async function fetchCoinGeckoPage(page) {
  try {
    return await jsonGet(`${CG_BASE}/coins/markets`, {
      vs_currency:            "usd",
      order:                  "volume_desc",
      per_page:               250,
      page,
      sparkline:              "false",
      price_change_percentage: "24h",
    }, { timeoutMs: CG_TIMEOUT_MS, headers: { "Accept": "application/json" } });
  } catch (e) {
    // 429 → wait 65 s then retry once
    if (String(e).includes("429")) {
      process.stderr.write("[CoinGecko] Rate-limited — waiting 65 s before retry\n");
      await sleep(65_000);
      return fetchCoinGeckoPage(page);
    }
    process.stderr.write(`[CoinGecko] Page ${page} failed: ${e.message}\n`);
    return null;
  }
}

async function fetchCoinGeckoTop() {
  const coins = [];
  for (let page = 1; page <= CG_PAGES; page++) {
    if (page > 1) await sleep(CG_PAGE_DELAY_MS);
    const data = await fetchCoinGeckoPage(page);
    if (!data) break;
    coins.push(...data);
  }
  return coins;
}

async function doRefresh() {
  process.stderr.write("[CoinSelector] Refreshing pair list…\n");

  // Step 1 — CoinGecko top coins by 24h volume
  const cgCoins = await fetchCoinGeckoTop();
  if (!cgCoins.length) {
    process.stderr.write("[CoinSelector] CoinGecko unavailable — keeping existing list\n");
    return; // leave _selectedPairs unchanged
  }

  // Step 2 — Haram filter
  const halal = cgCoins.filter(c => isHalal(c.symbol ?? "", c.name ?? ""));
  process.stderr.write(
    `[CoinSelector] ${cgCoins.length} → ${halal.length} coins after haram filter ` +
    `(${cgCoins.length - halal.length} removed)\n`
  );

  // Step 3 — MEXC active USDT spot pairs
  let mexcPairs;
  try {
    mexcPairs = await fetchMexcActivePairs();
  } catch (e) {
    process.stderr.write(`[CoinSelector] MEXC symbol fetch failed: ${e.message}\n`);
    mexcPairs = new Set();
  }

  // Step 4 — Cross-reference: keep only coins with an active MEXC USDT pair
  const candidates = halal
    .map(c => ({ ...c, mexcSymbol: `${c.symbol.toUpperCase()}USDT` }))
    .filter(c => mexcPairs.has(c.mexcSymbol));
  process.stderr.write(
    `[CoinSelector] Cross-reference: ${halal.length} halal coins × ${mexcPairs.size} MEXC USDT pairs ` +
    `→ ${candidates.length} matches\n`
  );
  if (candidates.length === 0 && mexcPairs.size > 0) {
    // Diagnostic sample to help debug future mismatches
    const sample = halal.slice(0, 5).map(c => `${c.symbol.toUpperCase()}USDT`);
    process.stderr.write(`[CoinSelector] Sample constructed symbols: ${sample.join(", ")}\n`);
    process.stderr.write(`[CoinSelector] Sample MEXC pairs: ${[...mexcPairs].slice(0, 5).join(", ")}\n`);
  }

  // Step 5 — Re-rank by MEXC 24h USDT volume
  let volMap = new Map();
  try {
    const tickers = await publicGet("/api/v3/ticker/24hr");
    volMap = new Map(
      (Array.isArray(tickers) ? tickers : [])
        .map(t => [t.symbol, parseFloat(t.quoteVolume ?? 0)])
    );
    candidates.sort((a, b) => (volMap.get(b.mexcSymbol) ?? 0) - (volMap.get(a.mexcSymbol) ?? 0));
  } catch (e) {
    process.stderr.write(`[CoinSelector] 24h ticker fetch failed: ${e.message} — using CoinGecko order\n`);
  }

  // Step 6 — Volume floor ($500k)
  const aboveFloor = candidates.filter(c => (volMap.get(c.mexcSymbol) ?? 0) >= MIN_MEXC_VOLUME);
  process.stderr.write(
    `[CoinSelector] Volume floor $${MIN_MEXC_VOLUME.toLocaleString()}: ` +
    `${candidates.length} → ${aboveFloor.length} pairs\n`
  );

  // Step 7 — Cap at MAX; supplement with fallbacks if below MIN
  let selected = aboveFloor.slice(0, MAX_SELECTED_PAIRS);

  if (selected.length < MIN_SELECTED_PAIRS) {
    process.stderr.write(
      `[CoinSelector] Only ${selected.length} pairs qualified — supplementing with fallbacks\n`
    );
    for (const sym of FALLBACK_PAIRS) {
      if (selected.length >= MIN_SELECTED_PAIRS) break;
      if (!selected.find(c => c.mexcSymbol === sym) && mexcPairs.has(sym)) {
        selected.push({ mexcSymbol: sym, symbol: sym.replace("USDT", ""), name: sym, total_volume: 0 });
      }
    }
  }

  // Normalise into a stable shape for the cache
  _selectedPairs = selected.map(c => ({
    mexcSymbol:       c.mexcSymbol,
    cgSymbol:         (c.symbol ?? "").toUpperCase(),
    name:             c.name ?? c.mexcSymbol,
    volume24hUsd:     volMap.get(c.mexcSymbol) ?? c.total_volume ?? 0,
    priceChangePct24h: c.price_change_percentage_24h ?? null,
  }));
  _selectorExpiry = Date.now() + SELECTOR_TTL_MS;

  const symbols = _selectedPairs.map(p => p.mexcSymbol);
  process.stderr.write(`[CoinSelector] Active pairs (${symbols.length}): ${symbols.join(", ")}\n`);
  process.stderr.write(
    `[CoinSelector] Next refresh at ${new Date(_selectorExpiry).toISOString()}\n`
  );
}

/** Returns the cached halal pair list, triggering a refresh when stale. */
async function getSelectedPairs() {
  if (!_selectedPairs || Date.now() > _selectorExpiry) {
    if (!_refreshPromise) {
      _refreshPromise = doRefresh()
        .catch(e => process.stderr.write(`[CoinSelector] Refresh error: ${e.message}\n`))
        .finally(() => { _refreshPromise = null; });
    }
    await _refreshPromise;
    // If refresh produced nothing (e.g. all APIs down), return fallback shape
    if (!_selectedPairs) {
      _selectedPairs = FALLBACK_PAIRS.map(sym => ({
        mexcSymbol: sym, cgSymbol: sym.replace("USDT", ""),
        name: sym, volume24hUsd: 0, priceChangePct24h: null,
      }));
      _selectorExpiry = Date.now() + 30 * 60 * 1000; // retry in 30 min on failure
    }
  }
  return _selectedPairs;
}

// ─────────────────────────────────────────────────────────────────────────────
//  Tool handlers
// ─────────────────────────────────────────────────────────────────────────────

function credsOk()      { return Boolean(API_KEY && API_SECRET); }
function requireCreds() { if (!credsOk()) throw new Error("MEXC_API_KEY / MEXC_SECRET not set in .env"); }

async function handle_get_balance() {
  requireCreds();
  const data   = await privateGet("/api/v3/account");
  const usdt   = (data.balances ?? []).find(b => b.asset === "USDT") ?? { free: "0", locked: "0" };
  const free   = parseFloat(usdt.free), locked = parseFloat(usdt.locked);
  return { asset: "USDT", free: usdt.free, locked: usdt.locked,
           total: (free + locked).toFixed(8), can_trade: data.canTrade ?? false };
}

async function handle_get_active_pairs({ limit = MAX_SELECTED_PAIRS, force_refresh = false } = {}) {
  if (force_refresh) {
    _selectedPairs  = null;
    _selectorExpiry = 0;
  }
  const pairs = await getSelectedPairs();
  const cap   = Math.min(Math.max(1, Math.floor(limit)), MAX_SELECTED_PAIRS);
  return {
    count:        Math.min(pairs.length, cap),
    refresh_due:  new Date(_selectorExpiry).toISOString(),
    pairs:        pairs.slice(0, cap).map((p, i) => ({
      rank:              i + 1,
      symbol:            p.mexcSymbol,
      name:              p.name,
      ticker:            p.cgSymbol,
      volume_24h_usdt:   Math.round(p.volume24hUsd),
      price_change_24h:  p.priceChangePct24h !== null
                           ? `${p.priceChangePct24h.toFixed(2)}%`
                           : null,
    })),
  };
}

async function handle_evaluate_signal({ symbol, timeframe = "60m" } = {}) {
  const VALID_TF = new Set(["1m", "5m", "15m", "30m", "60m", "4h", "1d", "1W", "1M"]);
  if (!VALID_TF.has(timeframe))
    throw new Error(`Invalid timeframe '${timeframe}'. Valid: ${[...VALID_TF].join(", ")}`);

  const sym     = symbol.toUpperCase();
  const raw     = await publicGet("/api/v3/klines", { symbol: sym, interval: timeframe, limit: SCAN_CANDLES });
  const candles = parseKlines(raw);
  if (candles.length < 30) throw new Error(`Not enough candle data for ${sym}: got ${candles.length}`);

  const n = candles.length, price = candles[n - 1].close;
  const signal  = generateSignal(sym, candles);
  const window  = Math.min(n, OB_LOOKBACK * 2), slice = candles.slice(-window);
  const fvgList = detectFVGs(slice), obList = detectOrderBlocks(slice);

  const stats = {
    active_bull_fvgs: fvgList.filter(f => f.type === "bullish" && !f.filled).length,
    active_bull_obs:  obList.filter(o => o.type === "bullish"  && !o.mitigated).length,
  };

  if (!signal) {
    return { symbol: sym, timeframe, current_price: price, signal_found: false,
             zone_type: null, ...stats,
             recommendation: "No valid setup — price is not inside any active bullish zone." };
  }
  return {
    symbol: sym, timeframe, current_price: price, signal_found: true, side: "BUY",
    zone_type:     signal.zoneType,
    strength:      parseFloat(signal.strength.toFixed(3)),
    entry_price:   signal.entryPrice,
    stop_loss:     parseFloat(signal.stopLoss.toFixed(8)),
    take_profit:   parseFloat(signal.takeProfit.toFixed(8)),
    risk_reward:   TP_RR,
    ...stats,
    recommendation: `BUY signal — price entering ${signal.zoneType} zone ` +
                    `(strength ${(signal.strength * 100).toFixed(0)}%). ` +
                    `SL: ${signal.stopLoss.toFixed(4)} | TP: ${signal.takeProfit.toFixed(4)}`,
  };
}

async function handle_execute_trade({ symbol, side, quantity }) {
  requireCreds();
  const sideU = String(side).toUpperCase();
  if (!["BUY", "SELL"].includes(sideU)) throw new Error("side must be BUY or SELL");
  if (!(quantity > 0))                   throw new Error("quantity must be > 0");
  const order = await privatePost("/api/v3/order",
    { symbol: symbol.toUpperCase(), side: sideU, type: "MARKET", quantity });
  return {
    order_id:      order.orderId,
    symbol:        order.symbol,
    side:          order.side,
    type:          order.type,
    status:        order.status,
    quantity:      order.origQty,
    filled:        order.executedQty,
    avg_price:     order.price,
    transact_time: order.transactTime ? new Date(order.transactTime).toISOString() : null,
  };
}

async function handle_get_open_orders({ symbol } = {}) {
  requireCreds();
  const raw    = await privateGet("/api/v3/openOrders", symbol ? { symbol: symbol.toUpperCase() } : {});
  const orders = (Array.isArray(raw) ? raw : []).map(o => {
    const orig = parseFloat(o.origQty ?? 0), exec = parseFloat(o.executedQty ?? 0);
    return { order_id: o.orderId, symbol: o.symbol, side: o.side, type: o.type,
             status: o.status, quantity: o.origQty, filled: o.executedQty,
             remaining: (orig - exec).toFixed(8), price: o.price,
             placed_at: o.time ? new Date(o.time).toISOString() : null };
  });
  return { count: orders.length, orders };
}

async function handle_cancel_order({ order_id, symbol }) {
  requireCreds();
  const raw  = await privateDelete("/api/v3/order",
    { symbol: symbol.toUpperCase(), orderId: order_id });
  const orig = parseFloat(raw.origQty ?? 0), exec = parseFloat(raw.executedQty ?? 0);
  return { order_id: raw.orderId, symbol: raw.symbol, side: raw.side, status: raw.status,
           orig_quantity: raw.origQty, filled_quantity: raw.executedQty,
           remaining: (orig - exec).toFixed(8), price: raw.price };
}

// ─────────────────────────────────────────────────────────────────────────────
//  Tool definitions
// ─────────────────────────────────────────────────────────────────────────────

const TOOLS = [
  {
    name: "get_balance",
    description: "Fetch the USDT spot account balance (free, locked, total). Requires API key with Read permission.",
    inputSchema: { type: "object", properties: {}, required: [] },
  },
  {
    name: "get_active_pairs",
    description:
      "Return the current halal-filtered top trading pairs. " +
      "Pairs are selected by fetching the top 500 coins from CoinGecko by 24h volume, " +
      "removing haram coins (gambling, adult, alcohol, weapons, riba/lending protocols), " +
      "cross-referencing with active MEXC USDT spot pairs, re-ranking by MEXC 24h volume, " +
      "applying a $500k volume floor, and capping at 20 pairs. " +
      "Results are cached for 4 hours. Pass force_refresh=true to trigger an immediate refresh.",
    inputSchema: {
      type: "object",
      properties: {
        limit: {
          type: "number",
          description: `Max pairs to return (1–${MAX_SELECTED_PAIRS}). Default ${MAX_SELECTED_PAIRS}.`,
          default: MAX_SELECTED_PAIRS,
        },
        force_refresh: {
          type: "boolean",
          description: "Set true to bypass the cache and fetch fresh data immediately.",
          default: false,
        },
      },
      required: [],
    },
  },
  {
    name: "evaluate_signal",
    description:
      "Fetch recent OHLCV data for ANY symbol and evaluate whether a valid SMC buy signal exists. " +
      "Detects Fair Value Gaps (FVG) and Order Blocks (OB), scores confluence, " +
      "and returns entry / stop-loss / take-profit levels. " +
      "Works with any symbol returned by get_active_pairs or any other MEXC USDT pair.",
    inputSchema: {
      type: "object",
      properties: {
        symbol: {
          type: "string",
          description: 'MEXC trading pair, e.g. "BTCUSDT" or any pair from get_active_pairs.',
        },
        timeframe: {
          type: "string",
          description: 'Candle width. Valid: 1m 5m 15m 30m 60m 4h 1d 1W 1M. Default "60m". Note: MEXC uses "60m" not "1h".',
          default: "60m",
        },
      },
      required: ["symbol"],
    },
  },
  {
    name: "execute_trade",
    description:
      "Place a spot MARKET order (BUY or SELL). Spot only — no futures, no leverage. Requires Trade permission.",
    inputSchema: {
      type: "object",
      properties: {
        symbol:   { type: "string",  description: 'MEXC trading pair, e.g. "BTCUSDT".' },
        side:     { type: "string",  enum: ["BUY", "SELL"], description: '"BUY" or "SELL".' },
        quantity: { type: "number",  description: "Base-asset quantity (e.g. BTC amount for BTCUSDT)." },
      },
      required: ["symbol", "side", "quantity"],
    },
  },
  {
    name: "get_open_orders",
    description: "List open spot orders. Optionally filtered to one symbol. Requires Read permission.",
    inputSchema: {
      type: "object",
      properties: {
        symbol: { type: "string", description: 'Filter by pair, e.g. "BTCUSDT". Leave empty for all.' },
      },
      required: [],
    },
  },
  {
    name: "cancel_order",
    description: "Cancel an open order by its ID. Requires Trade permission.",
    inputSchema: {
      type: "object",
      properties: {
        order_id: { type: "string", description: "Order ID from execute_trade or get_open_orders." },
        symbol:   { type: "string", description: 'Pair the order is on, e.g. "BTCUSDT".' },
      },
      required: ["order_id", "symbol"],
    },
  },
];

// ─────────────────────────────────────────────────────────────────────────────
//  MCP server
// ─────────────────────────────────────────────────────────────────────────────

const server = new Server(
  { name: "mexc-spot-smc", version: "2.0.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args = {} } = req.params;
  try {
    let result;
    switch (name) {
      case "get_balance":      result = await handle_get_balance();           break;
      case "get_active_pairs": result = await handle_get_active_pairs(args);  break;
      case "evaluate_signal":  result = await handle_evaluate_signal(args);   break;
      case "execute_trade":    result = await handle_execute_trade(args);     break;
      case "get_open_orders":  result = await handle_get_open_orders(args);   break;
      case "cancel_order":     result = await handle_cancel_order(args);      break;
      default:
        return { content: [{ type: "text", text: JSON.stringify({ error: `Unknown tool: ${name}` }) }], isError: true };
    }
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  } catch (e) {
    return { content: [{ type: "text", text: JSON.stringify({ error: e.message ?? String(e) }, null, 2) }], isError: true };
  }
});

// ─────────────────────────────────────────────────────────────────────────────
//  Start
// ─────────────────────────────────────────────────────────────────────────────

if (!credsOk()) {
  process.stderr.write(
    "[mexc-smc] WARNING: credentials missing — public tools work, private tools will fail.\n"
  );
}

// Kick off the first pair-list refresh in the background so it's ready
// before the first get_active_pairs call.
getSelectedPairs().catch(() => {});

await server.connect(new StdioServerTransport());
