/**
 * MEXC Trading Monitor — Express + Socket.IO server
 * ==================================================
 * Serves the live trading dashboard at http://localhost:3000
 * Protected with HTTP Basic Auth (set DASH_USER / DASH_PASS in .env).
 *
 * Endpoints
 * ---------
 *   GET  /                  — dashboard HTML
 *   GET  /api/live          — current snapshot (balance, positions, metrics)
 *   GET  /api/journal       — full trade journal
 *   POST /api/trades        — log a new trade
 *   PATCH /api/trades/:id   — update / close a trade
 *   GET  /api/export/csv    — download all trades as CSV
 *   GET  /api/report/daily  — today's summary JSON
 *
 * WebSocket (Socket.IO)
 * ---------------------
 *   event "data" — pushed every 30 s; same shape as GET /api/live
 */

import express           from "express";
import { createServer }  from "node:http";
import { Server as IO }  from "socket.io";
import basicAuth         from "express-basic-auth";
import crypto            from "node:crypto";
import fs                from "node:fs";
import path              from "node:path";
import { fileURLToPath } from "node:url";
import cron              from "node-cron";
import dotenv            from "dotenv";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: path.join(__dirname, ".env") });

// ── Config ────────────────────────────────────────────────────────────────────
const PORT          = parseInt(process.env.PORT          ?? "3000", 10);
const API_KEY       = process.env.MEXC_API_KEY            ?? "";
const API_SECRET    = process.env.MEXC_SECRET             ?? "";
const DASH_USER     = process.env.DASH_USER               ?? "admin";
const DASH_PASS     = process.env.DASH_PASS               ?? "changeme123";
const BASE_URL      = "https://api.mexc.com";
const JOURNAL_FILE  = path.join(__dirname, "trade-journal.json");
const REPORTS_DIR   = path.join(__dirname, "reports");
const POLL_MS       = 30_000;   // live data refresh interval

if (!fs.existsSync(REPORTS_DIR)) fs.mkdirSync(REPORTS_DIR, { recursive: true });

// ── MEXC API layer ────────────────────────────────────────────────────────────
function sign(qs) {
  return crypto.createHmac("sha256", API_SECRET).update(qs).digest("hex");
}
function stamp(params) {
  const p = { ...params, timestamp: Date.now(), recvWindow: 5000 };
  p.signature = sign(new URLSearchParams(p).toString());
  return p;
}
async function privateGet(endpoint, params = {}) {
  if (!API_KEY || !API_SECRET) throw new Error("Credentials not configured");
  const qs   = new URLSearchParams(stamp(params)).toString();
  const resp = await fetch(`${BASE_URL}${endpoint}?${qs}`, {
    headers: { "X-MEXC-APIKEY": API_KEY, Accept: "application/json" },
    signal:  AbortSignal.timeout(10_000),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(`MEXC ${body.code ?? resp.status}: ${body.msg ?? resp.statusText}`);
  return body;
}
async function publicGet(endpoint, params = {}) {
  const qs   = new URLSearchParams(params).toString();
  const resp = await fetch(qs ? `${BASE_URL}${endpoint}?${qs}` : `${BASE_URL}${endpoint}`, {
    headers: { Accept: "application/json" },
    signal:  AbortSignal.timeout(10_000),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(`MEXC ${body.code ?? resp.status}: ${body.msg ?? resp.statusText}`);
  return body;
}

// ── Trade journal ─────────────────────────────────────────────────────────────
function emptyJournal() {
  return {
    account:          { starting_balance: 0, currency: "USDT", created_at: new Date().toISOString() },
    trades:           [],
    equity_snapshots: [],
  };
}
function loadJournal() {
  if (!fs.existsSync(JOURNAL_FILE)) return emptyJournal();
  try   { return JSON.parse(fs.readFileSync(JOURNAL_FILE, "utf8")); }
  catch { return emptyJournal(); }
}
function saveJournal(j) {
  fs.writeFileSync(JOURNAL_FILE, JSON.stringify(j, null, 2));
}

// ── Metrics calculation ───────────────────────────────────────────────────────
function calcMetrics(journal, currentEquity) {
  const closed = journal.trades.filter(t => t.status === "CLOSED");
  const today  = new Date().toISOString().slice(0, 10);

  if (!closed.length) {
    return {
      total_trades: 0, win_rate: "0.0", profit_factor: "0.00",
      total_pnl_usdt: 0, total_pnl_pct: 0,
      today_pnl_usdt: 0, today_trades: 0, today_wins: 0,
      max_drawdown_pct: 0, sharpe_ratio: 0,
      avg_win_usdt: 0, avg_loss_usdt: 0,
    };
  }

  const wins        = closed.filter(t => (t.pnl_usdt ?? 0) > 0);
  const losses      = closed.filter(t => (t.pnl_usdt ?? 0) <= 0);
  const todayTrades = closed.filter(t => t.exit_timestamp?.startsWith(today));
  const grossProfit = wins.reduce((s, t)   => s + t.pnl_usdt, 0);
  const grossLoss   = losses.reduce((s, t) => s + Math.abs(t.pnl_usdt), 0);
  const totalPnl    = grossProfit - grossLoss;
  const todayPnl    = todayTrades.reduce((s, t) => s + (t.pnl_usdt ?? 0), 0);

  // Max drawdown from equity snapshots
  const snaps = journal.equity_snapshots;
  let maxDD = 0, peak = journal.account.starting_balance || currentEquity || 1;
  for (const s of snaps) {
    if (s.equity > peak) peak = s.equity;
    const dd = (peak - s.equity) / peak * 100;
    if (dd > maxDD) maxDD = dd;
  }

  // Sharpe (annualised) from daily equity snapshots
  const dayMap = new Map();
  for (const s of snaps) dayMap.set(s.timestamp.slice(0, 10), s.equity);
  const days    = [...dayMap.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  const returns = [];
  for (let i = 1; i < days.length; i++) {
    const prev = days[i - 1][1];
    if (prev > 0) returns.push((days[i][1] - prev) / prev);
  }
  let sharpe = 0;
  if (returns.length > 1) {
    const mean = returns.reduce((a, b) => a + b, 0) / returns.length;
    const std  = Math.sqrt(returns.reduce((s, r) => s + (r - mean) ** 2, 0) / returns.length);
    if (std > 0) sharpe = (mean / std) * Math.sqrt(252);
  }

  const startBal = journal.account.starting_balance || currentEquity || 1;

  return {
    total_trades:     closed.length,
    win_rate:         ((wins.length / closed.length) * 100).toFixed(1),
    profit_factor:    grossLoss > 0 ? (grossProfit / grossLoss).toFixed(2) : grossProfit > 0 ? "∞" : "0.00",
    total_pnl_usdt:   parseFloat(totalPnl.toFixed(2)),
    total_pnl_pct:    parseFloat((totalPnl / startBal * 100).toFixed(2)),
    today_pnl_usdt:   parseFloat(todayPnl.toFixed(2)),
    today_trades:     todayTrades.length,
    today_wins:       todayTrades.filter(t => (t.pnl_usdt ?? 0) > 0).length,
    max_drawdown_pct: parseFloat(maxDD.toFixed(2)),
    sharpe_ratio:     parseFloat(sharpe.toFixed(3)),
    avg_win_usdt:     wins.length   ? parseFloat((grossProfit / wins.length).toFixed(2))   : 0,
    avg_loss_usdt:    losses.length ? parseFloat((-grossLoss  / losses.length).toFixed(2)) : 0,
  };
}

// Builds series for charts: equity curve + daily returns
function buildChartData(journal) {
  const snaps = journal.equity_snapshots.slice(-90); // last 90 snapshots
  const equity = snaps.map(s => ({ x: s.timestamp.slice(0, 10), y: parseFloat(s.equity.toFixed(2)) }));

  const dayMap = new Map();
  for (const s of snaps) dayMap.set(s.timestamp.slice(0, 10), s.equity);
  const days = [...dayMap.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  const dailyReturns = [];
  for (let i = 1; i < days.length; i++) {
    const prev = days[i - 1][1];
    dailyReturns.push({
      x: days[i][0],
      y: prev > 0 ? parseFloat(((days[i][1] - prev) / prev * 100).toFixed(3)) : 0,
    });
  }

  return { equity, dailyReturns };
}

// ── Live state ────────────────────────────────────────────────────────────────
let live = {
  balance: null, positions: [], open_orders: [],
  recent_trades: [], metrics: null, charts: { equity: [], dailyReturns: [] },
  last_update: null, credentials_ok: false, error: null,
};

async function fetchLive() {
  const journal = loadJournal();
  const credOk  = Boolean(API_KEY && API_SECRET);
  let balanceUsdt = 0, rawBalances = [], positions = [], openOrders = [];

  if (credOk) {
    // Account balance
    try {
      const acct   = await privateGet("/api/v3/account");
      rawBalances  = (acct.balances ?? []).filter(
        b => parseFloat(b.free) + parseFloat(b.locked) > 0
      );
      const usdt    = rawBalances.find(b => b.asset === "USDT") ?? { free: "0", locked: "0" };
      balanceUsdt   = parseFloat(usdt.free) + parseFloat(usdt.locked);

      // Daily equity snapshot (once per day)
      const today   = new Date().toISOString().slice(0, 10);
      const snaps   = journal.equity_snapshots;
      const lastDay = snaps.at(-1)?.timestamp.slice(0, 10);
      if (lastDay !== today) {
        snaps.push({ timestamp: new Date().toISOString(), equity: balanceUsdt });
        saveJournal(journal);
      }
    } catch (e) {
      console.error("[Poll] balance:", e.message);
    }

    // Open orders
    try {
      const raw  = await privateGet("/api/v3/openOrders");
      openOrders = (Array.isArray(raw) ? raw : []).map(o => ({
        order_id: o.orderId, symbol: o.symbol, side: o.side, type: o.type,
        quantity: o.origQty, price: o.price, filled: o.executedQty,
        placed_at: o.time ? new Date(o.time).toISOString() : null,
        status: o.status,
      }));
    } catch (e) {
      console.error("[Poll] open orders:", e.message);
    }

    // Non-USDT balances → open positions with live prices
    for (const bal of rawBalances.filter(b => b.asset !== "USDT")) {
      try {
        const sym    = `${bal.asset}USDT`;
        const ticker = await publicGet("/api/v3/ticker/price", { symbol: sym });
        const px     = parseFloat(ticker.price ?? 0);
        const qty    = parseFloat(bal.free) + parseFloat(bal.locked);
        const entry  = journal.trades.filter(t => t.symbol === sym && t.status === "OPEN").at(-1)?.entry_price ?? px;
        const cost   = entry * qty;
        const val    = px * qty;
        positions.push({
          asset: bal.asset, symbol: sym,
          quantity: qty, free: parseFloat(bal.free), locked: parseFloat(bal.locked),
          entry_price: entry, current_price: px,
          cost_basis: parseFloat(cost.toFixed(2)), current_value: parseFloat(val.toFixed(2)),
          pnl_usdt: parseFloat((val - cost).toFixed(2)),
          pnl_pct:  parseFloat(cost > 0 ? ((val - cost) / cost * 100).toFixed(2) : "0"),
        });
      } catch { /* pair may not exist */ }
    }
  }

  const metrics = calcMetrics(journal, balanceUsdt);
  const charts  = buildChartData(journal);

  live = {
    balance: {
      usdt:                  parseFloat(balanceUsdt.toFixed(2)),
      total_position_value:  parseFloat(positions.reduce((s, p) => s + p.current_value, 0).toFixed(2)),
      total_equity:          parseFloat((balanceUsdt + positions.reduce((s, p) => s + p.current_value, 0)).toFixed(2)),
      assets:                rawBalances.map(b => ({ asset: b.asset, free: b.free, locked: b.locked })),
    },
    positions,
    open_orders:    openOrders,
    recent_trades:  journal.trades.slice(-100).reverse(),
    metrics,
    charts,
    last_update:    new Date().toISOString(),
    credentials_ok: credOk,
    error:          null,
  };
  return live;
}

// ── Daily report ──────────────────────────────────────────────────────────────
function generateDailyReport() {
  const journal = loadJournal();
  const today   = new Date().toISOString().slice(0, 10);
  const day     = journal.trades.filter(t => t.status === "CLOSED" && t.exit_timestamp?.startsWith(today));
  const wins    = day.filter(t => (t.pnl_usdt ?? 0) > 0);
  const pnl     = day.reduce((s, t) => s + (t.pnl_usdt ?? 0), 0);
  const report  = {
    date: today, generated_at: new Date().toISOString(),
    trades: day.length, wins: wins.length, losses: day.length - wins.length,
    win_rate:  day.length ? ((wins.length / day.length) * 100).toFixed(1) + "%" : "N/A",
    total_pnl: pnl.toFixed(2) + " USDT",
    details:   day,
  };
  const file = path.join(REPORTS_DIR, `report-${today}.json`);
  fs.writeFileSync(file, JSON.stringify(report, null, 2));
  console.log(`[Report] Saved ${file}`);
  return report;
}

// ── CSV export ─────────────────────────────────────────────────────────────────
function toCSV(trades) {
  const COLS = ["timestamp","symbol","side","quantity","entry_price","exit_price",
                "pnl_usdt","pnl_pct","zone_type","signal_strength","status","order_id"];
  return [COLS.join(","),
    ...trades.map(t => COLS.map(c => JSON.stringify(t[c] ?? "")).join(","))
  ].join("\n");
}

// ── Express + Socket.IO ───────────────────────────────────────────────────────
const app    = express();
const server = createServer(app);
const io     = new IO(server, { cors: { origin: "*" } });

app.use(express.json());

// Basic auth: protect all HTTP routes; socket.io uses its own upgrade path
app.use((req, res, next) => {
  if (req.path.startsWith("/socket.io")) return next();
  return basicAuth({ users: { [DASH_USER]: DASH_PASS }, challenge: true, realm: "MEXC Monitor" })(req, res, next);
});

app.get("/",              (_, res) => res.sendFile(path.join(__dirname, "trading-dashboard.html")));
app.get("/api/live",      (_, res) => res.json(live));
app.get("/api/journal",   (_, res) => res.json(loadJournal()));

app.post("/api/trades", (req, res) => {
  const journal = loadJournal();
  const trade   = { id: crypto.randomUUID(), timestamp: new Date().toISOString(), status: "OPEN", ...req.body };
  journal.trades.push(trade);
  saveJournal(journal);
  res.json({ ok: true, trade });
});

app.patch("/api/trades/:id", (req, res) => {
  const journal = loadJournal();
  const i       = journal.trades.findIndex(t => t.id === req.params.id);
  if (i === -1) return res.status(404).json({ error: "Not found" });
  journal.trades[i] = { ...journal.trades[i], ...req.body };
  saveJournal(journal);
  res.json({ ok: true, trade: journal.trades[i] });
});

app.delete("/api/trades/:id", (req, res) => {
  const journal = loadJournal();
  const before  = journal.trades.length;
  journal.trades = journal.trades.filter(t => t.id !== req.params.id);
  saveJournal(journal);
  res.json({ ok: true, removed: before - journal.trades.length });
});

app.get("/api/export/csv", (_, res) => {
  const journal = loadJournal();
  res.setHeader("Content-Type", "text/csv");
  res.setHeader("Content-Disposition", `attachment; filename="trades-${new Date().toISOString().slice(0,10)}.csv"`);
  res.send(toCSV(journal.trades));
});

app.get("/api/report/daily", (_, res) => res.json(generateDailyReport()));

// Socket.IO: push live data to every connected client
io.on("connection", socket => {
  console.log(`[WS] connected ${socket.id}`);
  socket.emit("data", live);
  socket.on("disconnect", () => console.log(`[WS] disconnected ${socket.id}`));
});

// ── Polling & cron ────────────────────────────────────────────────────────────
async function poll() {
  try {
    await fetchLive();
  } catch (e) {
    live.error       = e.message;
    live.last_update = new Date().toISOString();
  }
  io.emit("data", live);
}

setInterval(poll, POLL_MS);

// Daily report at 23:59 server time
cron.schedule("59 23 * * *", () => {
  try { generateDailyReport(); } catch (e) { console.error("[Cron]", e.message); }
});

// ── Boot ──────────────────────────────────────────────────────────────────────
await poll(); // first fetch before accepting connections

server.listen(PORT, "0.0.0.0", () => {
  console.log(`[Monitor] http://localhost:${PORT}  user=${DASH_USER}`);
  if (!API_KEY) console.warn("[Monitor] WARNING: MEXC_API_KEY not set — balance/positions unavailable");
});
