/**
 * Debug script for coin selector cross-reference issue.
 * Run: node debug_coin_selector.js
 */

const CG_BASE  = "https://api.coingecko.com/api/v3";
const BASE_URL = "https://api.mexc.com";

async function jsonGet(url, params = {}) {
  const qs      = new URLSearchParams(params).toString();
  const fullUrl = qs ? `${url}?${qs}` : url;
  const resp    = await fetch(fullUrl, {
    headers: { Accept: "application/json" },
    signal: AbortSignal.timeout(20_000),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(`HTTP ${body.code ?? resp.status}: ${body.msg ?? resp.statusText}`);
  return body;
}

// ── Step 1: CoinGecko sample ─────────────────────────────────────────────────
async function debugCoinGecko() {
  console.log("\n════════════════════════════════════════");
  console.log("STEP 1 — CoinGecko /coins/markets (page 1)");
  console.log("════════════════════════════════════════");

  let data;
  try {
    data = await jsonGet(`${CG_BASE}/coins/markets`, {
      vs_currency: "usd", order: "volume_desc", per_page: 10, page: 1,
      sparkline: "false", price_change_percentage: "24h",
    });
  } catch (e) {
    console.error("  CoinGecko FAILED:", e.message);
    return null;
  }

  if (!Array.isArray(data) || data.length === 0) {
    console.error("  CoinGecko returned unexpected shape:", JSON.stringify(data).slice(0, 200));
    return null;
  }

  console.log(`  Returned array of ${data.length} items. First 5 entries:`);
  for (const c of data.slice(0, 5)) {
    console.log(`    id="${c.id}"  symbol="${c.symbol}"  name="${c.name}"  volume=${c.total_volume}`);
  }

  console.log("\n  Keys on first entry:", Object.keys(data[0]).join(", "));

  // Show what mexcSymbol the code would construct
  console.log("\n  Constructed mexcSymbol for first 5:");
  for (const c of data.slice(0, 5)) {
    const mexcSym = `${(c.symbol ?? "").toUpperCase()}USDT`;
    console.log(`    CG symbol="${c.symbol}" → mexcSymbol="${mexcSym}"`);
  }

  return data;
}

// ── Step 2: MEXC exchangeInfo sample ────────────────────────────────────────
async function debugMexcExchangeInfo() {
  console.log("\n════════════════════════════════════════");
  console.log("STEP 2 — MEXC /api/v3/exchangeInfo");
  console.log("════════════════════════════════════════");

  let info;
  try {
    info = await jsonGet(`${BASE_URL}/api/v3/exchangeInfo`);
  } catch (e) {
    console.error("  MEXC exchangeInfo FAILED:", e.message);
    return null;
  }

  console.log("  Top-level keys:", Object.keys(info).join(", "));
  console.log("  Is top-level array:", Array.isArray(info));

  // Figure out where the symbol array lives
  let symbols;
  if (Array.isArray(info)) {
    symbols = info;
    console.log("  → using top-level array directly");
  } else if (Array.isArray(info.symbols)) {
    symbols = info.symbols;
    console.log(`  → using info.symbols (${symbols.length} entries)`);
  } else {
    // Look for any array-valued key
    for (const [k, v] of Object.entries(info)) {
      if (Array.isArray(v)) {
        console.log(`  → found array at key "${k}" with ${v.length} entries`);
      }
    }
    console.error("  Neither info nor info.symbols is an array — nothing to process");
    return null;
  }

  if (symbols.length === 0) {
    console.error("  Symbols array is empty!");
    return null;
  }

  // Show raw shape of first symbol
  console.log("\n  First symbol (raw):", JSON.stringify(symbols[0], null, 2));
  console.log("\n  Keys on symbol entries:", Object.keys(symbols[0]).join(", "));

  // Count by status
  const statusCounts = {};
  for (const s of symbols) {
    statusCounts[s.status ?? "undefined"] = (statusCounts[s.status ?? "undefined"] ?? 0) + 1;
  }
  console.log("\n  Status value distribution:", JSON.stringify(statusCounts));

  // Count by quoteAsset
  const quoteCounts = {};
  for (const s of symbols) {
    const q = s.quoteAsset ?? s.quoteCurrency ?? s.quoteToken ?? "UNKNOWN";
    quoteCounts[q] = (quoteCounts[q] ?? 0) + 1;
  }
  console.log("  Top quoteAsset values:", JSON.stringify(
    Object.entries(quoteCounts).sort((a, b) => b[1] - a[1]).slice(0, 10)
  ));

  // Filter USDT + ENABLED and show first few
  const usdtEnabled = symbols.filter(s => {
    const isUsdt = (s.quoteAsset ?? s.quoteCurrency ?? "") === "USDT";
    const status  = s.status ?? "";
    return isUsdt && (status === "ENABLED" || status === "TRADING");
  });
  console.log(`\n  USDT pairs with status ENABLED or TRADING: ${usdtEnabled.length}`);
  console.log("  First 5 filtered pairs:", usdtEnabled.slice(0, 5).map(s => s.symbol).join(", "));

  // Also try alternative: just look at symbol string ending in USDT
  const usdtSuffix = symbols.filter(s => typeof s.symbol === "string" && s.symbol.endsWith("USDT"));
  console.log(`  USDT pairs (by symbol suffix only): ${usdtSuffix.length}`);

  return { symbols, usdtEnabled };
}

// ── Step 3: Cross-reference test ─────────────────────────────────────────────
async function debugCrossReference(cgCoins, mexcData) {
  console.log("\n════════════════════════════════════════");
  console.log("STEP 3 — Cross-reference test");
  console.log("════════════════════════════════════════");

  if (!cgCoins || !mexcData) {
    console.log("  Skipping — previous steps failed");
    return;
  }

  const { symbols, usdtEnabled } = mexcData;

  // Build set the same way the server does
  const mexcSet = new Set(usdtEnabled.map(s => s.symbol));
  console.log(`  mexcSet size: ${mexcSet.size}`);
  console.log("  Sample mexcSet entries:", [...mexcSet].slice(0, 5).join(", "));

  // Attempt cross-reference for first 10 CoinGecko coins
  console.log("\n  Cross-reference check for first 10 CoinGecko coins:");
  let matchCount = 0;
  for (const c of cgCoins.slice(0, 10)) {
    const cgSym   = (c.symbol ?? "").toUpperCase();
    const target  = `${cgSym}USDT`;
    const matched = mexcSet.has(target);
    console.log(`    CG "${c.symbol}" → "${target}" — ${matched ? "MATCH ✓" : "NO MATCH ✗"}`);
    if (matched) matchCount++;
  }
  console.log(`\n  Matches among first 10: ${matchCount}`);

  // Try all 10 from CoinGecko against full 500 symbol set (any status)
  const allSymSet = new Set(symbols.map(s => s.symbol));
  console.log("\n  Re-check against ALL symbols (ignoring status) for first 10:");
  for (const c of cgCoins.slice(0, 10)) {
    const target  = `${(c.symbol ?? "").toUpperCase()}USDT`;
    const matched = allSymSet.has(target);
    console.log(`    "${target}" — ${matched ? "FOUND" : "not found"}`);
  }

  // Check if known-good coins are in the set
  const wellKnown = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"];
  console.log("\n  Well-known symbol lookup (mexcSet):");
  for (const sym of wellKnown) {
    console.log(`    ${sym}: ${mexcSet.has(sym) ? "YES ✓" : "NO ✗"}`);
  }
  console.log("  Well-known symbol lookup (allSymSet):");
  for (const sym of wellKnown) {
    console.log(`    ${sym}: ${allSymSet.has(sym) ? "YES ✓" : "NO ✗"}`);
  }
}

// ── Step 4: Alternative filter approach ─────────────────────────────────────
async function debugAlternativeFilter(mexcData) {
  console.log("\n════════════════════════════════════════");
  console.log("STEP 4 — Field name investigation");
  console.log("════════════════════════════════════════");

  if (!mexcData) { console.log("  Skipping"); return; }

  const { symbols } = mexcData;
  if (symbols.length === 0) return;

  // Show all field names that contain 'quote' or 'asset' (case-insensitive)
  const allKeys = new Set(symbols.flatMap(s => Object.keys(s)));
  const quoteKeys = [...allKeys].filter(k => k.toLowerCase().includes("quote") || k.toLowerCase().includes("asset"));
  console.log("  Keys containing 'quote' or 'asset':", quoteKeys.join(", "));

  // Show all unique statuses
  const statuses = new Set(symbols.map(s => s.status ?? s.state ?? "—"));
  console.log("  All status values:", [...statuses].join(", "));

  // Show what BTC entry looks like in full
  const btc = symbols.find(s => s.symbol === "BTCUSDT");
  if (btc) {
    console.log("\n  BTCUSDT entry:", JSON.stringify(btc, null, 2));
  } else {
    console.log("\n  BTCUSDT not found in symbols array!");
    console.log("  Searching for BTC variants:", symbols.filter(s => s.symbol?.includes("BTC")).slice(0, 5).map(s => s.symbol).join(", "));
  }
}

// ── Main ─────────────────────────────────────────────────────────────────────
(async () => {
  try {
    const cgCoins  = await debugCoinGecko();
    const mexcData = await debugMexcExchangeInfo();
    await debugCrossReference(cgCoins, mexcData);
    await debugAlternativeFilter(mexcData);
    console.log("\n════ Debug complete ════\n");
  } catch (e) {
    console.error("Unhandled error:", e);
  }
})();
