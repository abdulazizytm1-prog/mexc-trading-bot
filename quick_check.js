/**
 * Standalone check: balance + SMC signal evaluation for BTCUSDT and ETHUSDT.
 * Run: node quick_check.js
 */
import crypto            from "node:crypto";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import dotenv             from "dotenv";

const __dirname = dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: join(__dirname, ".env") });

const API_KEY    = process.env.MEXC_API_KEY ?? "";
const API_SECRET = process.env.MEXC_SECRET  ?? "";
const BASE_URL   = "https://api.mexc.com";
const RECV_WIN   = 5000;

// ── HTTP ─────────────────────────────────────────────────────────────────────
async function jsonGet(url, params = {}, headers = {}) {
  const qs   = new URLSearchParams(params).toString();
  const resp = await fetch(qs ? `${url}?${qs}` : url, {
    headers: { Accept: "application/json", ...headers },
    signal: AbortSignal.timeout(15_000),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(`HTTP ${body.code ?? resp.status}: ${body.msg ?? resp.statusText}`);
  return body;
}

function sign(qs) {
  return crypto.createHmac("sha256", API_SECRET).update(qs).digest("hex");
}
function stamp(params) {
  const p      = { ...params, timestamp: Date.now(), recvWindow: RECV_WIN };
  p.signature  = sign(new URLSearchParams(p).toString());
  return p;
}
async function privateGet(endpoint, params = {}) {
  const qs = new URLSearchParams(stamp(params)).toString();
  return jsonGet(`${BASE_URL}${endpoint}?${qs}`, {}, { "X-MEXC-APIKEY": API_KEY });
}

// ── SMC constants ─────────────────────────────────────────────────────────────
const ATR_PERIOD     = 14;
const ATR_SL_MULT    = 1.5;
const TP_RR          = 2.0;
const FVG_MIN_PCT    = 0.002;
const OB_MIN_IMPULSE = 0.005;
const OB_LOOKBACK    = 20;

function parseKlines(raw) {
  return raw.map(r => ({
    open: parseFloat(r[1]), high: parseFloat(r[2]),
    low:  parseFloat(r[3]), close: parseFloat(r[4]),
  }));
}

function calcATR(candles, period = ATR_PERIOD) {
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
  const fvgs = [], n = candles.length;
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
  for (const fvg of fvgs)
    for (let j = fvg.formedAt + 1; j < n; j++) {
      if (fvg.type === "bullish" && candles[j].low  <= fvg.bottom) { fvg.filled = true; break; }
      if (fvg.type === "bearish" && candles[j].high >= fvg.top)    { fvg.filled = true; break; }
    }
  return fvgs;
}

function detectOrderBlocks(candles) {
  const obs = [], n = candles.length, look = 3;
  for (let i = 1; i < n - look; i++) {
    const c = candles[i];
    if (c.close < c.open) {
      const impulse = (candles[i + look].close - c.close) / c.close;
      if (impulse >= OB_MIN_IMPULSE) {
        const clean = Array.from({ length: look - 1 }, (_, k) => candles[i + 1 + k])
          .every(x => x.close >= x.open);
        if (clean)
          obs.push({ type: "bullish", top: Math.max(c.open, c.close), bottom: c.low, formedAt: i, mitigated: false });
      }
    }
  }
  for (const ob of obs)
    for (let j = ob.formedAt + look + 1; j < n; j++)
      if (ob.type === "bullish" && candles[j].low <= ob.bottom) { ob.mitigated = true; break; }
  return obs;
}

function evaluateSignal(symbol, candles) {
  const n     = candles.length;
  const price = candles[n - 1].close;
  const atr   = calcATR(candles);
  const slice = candles.slice(-OB_LOOKBACK * 2);
  const len   = slice.length;

  const bullFVG = detectFVGs(slice).filter(f => f.type === "bullish" && !f.filled);
  const bullOB  = detectOrderBlocks(slice).filter(o => o.type === "bullish" && !o.mitigated);

  const recency = (formedAt) => Math.max(0, 1 - (len - formedAt) / Math.max(len, 1));

  let best = null, bestStrength = 0;
  const makeSignal = (bottom, zoneType, strength) => {
    const sl = Math.max(bottom - atr * ATR_SL_MULT, 0);
    return { symbol, side: "BUY", zoneType, strength, entryPrice: price,
             stopLoss: sl, takeProfit: price + (price - sl) * TP_RR };
  };

  for (const fvg of bullFVG) {
    if (fvg.bottom <= price && price <= fvg.top) {
      const s = recency(fvg.formedAt);
      if (s > bestStrength) { bestStrength = s; best = makeSignal(fvg.bottom, "FVG", s); }
    }
  }
  for (const ob of bullOB) {
    if (ob.bottom <= price && price <= ob.top) {
      let s = Math.min(recency(ob.formedAt) + 0.15, 1.0);
      const confluence = bullFVG.some(f => !f.filled && !(f.top < ob.bottom || f.bottom > ob.top));
      if (confluence) s = Math.min(s + 0.20, 1.0);
      const zone = confluence ? "FVG+OB" : "OB";
      if (s > bestStrength) { bestStrength = s; best = makeSignal(ob.bottom, zone, s); }
    }
  }

  return {
    symbol, price,
    signal_found: best !== null,
    zone_type:    best?.zoneType ?? null,
    strength:     best ? parseFloat(best.strength.toFixed(3)) : null,
    entry:        best?.entryPrice ?? null,
    stop_loss:    best ? parseFloat(best.stopLoss.toFixed(6))   : null,
    take_profit:  best ? parseFloat(best.takeProfit.toFixed(6)) : null,
    risk_reward:  best ? TP_RR : null,
    active_bull_fvgs: bullFVG.length,
    active_bull_obs:  bullOB.length,
    atr: parseFloat(atr.toFixed(6)),
  };
}

// ── Main ─────────────────────────────────────────────────────────────────────
(async () => {
  // ── Balance ────────────────────────────────────────────────────────────────
  console.log("══════════════════════════════════════");
  console.log("  MEXC SPOT BALANCE");
  console.log("══════════════════════════════════════");

  if (!API_KEY || !API_SECRET) {
    console.log("  ⚠  No credentials in .env — skipping balance check");
  } else {
    try {
      const data = await privateGet("/api/v3/account");
      const balances = (data.balances ?? []).filter(b =>
        parseFloat(b.free ?? 0) > 0 || parseFloat(b.locked ?? 0) > 0
      );
      const usdt = balances.find(b => b.asset === "USDT") ?? { free: "0", locked: "0" };
      console.log(`  USDT free   : ${parseFloat(usdt.free).toFixed(2)}`);
      console.log(`  USDT locked : ${parseFloat(usdt.locked).toFixed(2)}`);
      console.log(`  USDT total  : ${(parseFloat(usdt.free) + parseFloat(usdt.locked)).toFixed(2)}`);
      const others = balances.filter(b => b.asset !== "USDT");
      if (others.length) {
        console.log(`\n  Other non-zero assets:`);
        for (const b of others)
          console.log(`    ${b.asset.padEnd(8)} free=${parseFloat(b.free).toFixed(6)}  locked=${parseFloat(b.locked).toFixed(6)}`);
      }
      console.log(`\n  canTrade    : ${data.canTrade}`);
    } catch (e) {
      console.log("  Balance error:", e.message);
    }
  }

  // ── Signals ────────────────────────────────────────────────────────────────
  for (const sym of ["BTCUSDT", "ETHUSDT"]) {
    console.log(`\n══════════════════════════════════════`);
    console.log(`  SMC SIGNAL — ${sym} (60m)`);
    console.log(`══════════════════════════════════════`);

    try {
      const raw     = await jsonGet(`${BASE_URL}/api/v3/klines`, { symbol: sym, interval: "60m", limit: 100 });
      const candles = parseKlines(raw);
      const result  = evaluateSignal(sym, candles);

      console.log(`  Current price     : ${result.price}`);
      console.log(`  ATR (14)          : ${result.atr}`);
      console.log(`  Active bull FVGs  : ${result.active_bull_fvgs}`);
      console.log(`  Active bull OBs   : ${result.active_bull_obs}`);
      console.log(`  Signal found      : ${result.signal_found}`);

      if (result.signal_found) {
        console.log(`\n  ✅ BUY SIGNAL`);
        console.log(`  Zone type   : ${result.zone_type}`);
        console.log(`  Strength    : ${(result.strength * 100).toFixed(0)}%`);
        console.log(`  Entry       : ${result.entry}`);
        console.log(`  Stop loss   : ${result.stop_loss}`);
        console.log(`  Take profit : ${result.take_profit}`);
        console.log(`  R/R         : 1:${result.risk_reward}`);
      } else {
        console.log(`  ⏸  No setup — price not inside any active bullish zone`);
      }
    } catch (e) {
      console.log(`  Error: ${e.message}`);
    }
  }

  console.log("\n══════════════════════════════════════\n");
})();
