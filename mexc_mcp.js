/**
 * MEXC Spot Trading — MCP Server (Node.js / CCXT)
 * ================================================
 * Exposes 7 tools for MEXC spot trading via the Model Context Protocol.
 * Spot only — no futures, no leverage, no margin.
 *
 * Setup
 * -----
 *   npm install
 *   # Credentials are read from .env in the same directory:
 *   #   MEXC_API_KEY=your_key
 *   #   MEXC_SECRET=your_secret
 *   node mexc_mcp.js
 *
 * Tools
 * -----
 *   get_balance        — USDT free / locked / total          [private]
 *   get_ticker         — last price + 24 h stats              [public]
 *   get_ohlcv          — OHLCV candlestick data               [public]
 *   place_buy_order    — market buy (spot only)               [private]
 *   place_sell_order   — market sell (spot only)              [private]
 *   get_open_orders    — list open orders                     [private]
 *   cancel_order       — cancel an order by ID               [private]
 *
 * Symbol format
 * -------------
 *   CCXT uses slash notation: "BTC/USDT".
 *   This server also accepts exchange notation "BTCUSDT" and converts it.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import ccxt from "ccxt";
import dotenv from "dotenv";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

// ── Load .env relative to this file, not the process cwd ────────────────────
const __dirname = dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: join(__dirname, ".env") });

// ── Exchange setup ───────────────────────────────────────────────────────────
const API_KEY = process.env.MEXC_API_KEY ?? "";
const SECRET  = process.env.MEXC_SECRET  ?? "";

const exchange = new ccxt.mexc({
  apiKey: API_KEY,
  secret: SECRET,
  options: {
    defaultType: "spot",   // spot only — never futures
  },
});

// Pre-load markets once so symbol look-ups are fast.
// Errors here are non-fatal; tools will surface them individually.
let marketsLoaded = false;
async function ensureMarkets() {
  if (!marketsLoaded) {
    await exchange.loadMarkets();
    marketsLoaded = true;
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Convert "BTCUSDT" → "BTC/USDT" if no slash is present. */
function normalizeSymbol(raw) {
  if (raw.includes("/")) return raw.toUpperCase();
  // Heuristic: split before the last known quote currency.
  const quotes = ["USDT", "USDC", "BTC", "ETH", "BNB"];
  const up = raw.toUpperCase();
  for (const q of quotes) {
    if (up.endsWith(q)) return `${up.slice(0, -q.length)}/${q}`;
  }
  return up; // fall back to raw — CCXT will reject it with a clear error
}

const credsOk = () => Boolean(API_KEY && SECRET);

function ok(data) {
  return {
    content: [{ type: "text", text: JSON.stringify(data, null, 2) }],
  };
}

function err(message) {
  return {
    content: [{ type: "text", text: JSON.stringify({ error: String(message) }, null, 2) }],
    isError: true,
  };
}

function requireCreds() {
  if (!credsOk()) {
    throw new Error(
      "API credentials not set. Add MEXC_API_KEY and MEXC_SECRET to .env."
    );
  }
}

// ── Tool definitions ─────────────────────────────────────────────────────────

const TOOLS = [
  {
    name: "get_balance",
    description:
      "Fetch the USDT spot account balance (free, locked, and total). " +
      "Requires API key with Read permission.",
    inputSchema: {
      type: "object",
      properties: {},
      required: [],
    },
  },
  {
    name: "get_ticker",
    description:
      "Get the current price and 24-hour rolling statistics for a symbol. " +
      "Public endpoint — no API key required.",
    inputSchema: {
      type: "object",
      properties: {
        symbol: {
          type: "string",
          description: 'Trading pair. Accepts "BTC/USDT" or "BTCUSDT".',
        },
      },
      required: ["symbol"],
    },
  },
  {
    name: "get_ohlcv",
    description:
      "Fetch OHLCV (candlestick) data for a symbol. Public endpoint.",
    inputSchema: {
      type: "object",
      properties: {
        symbol: {
          type: "string",
          description: 'Trading pair, e.g. "BTC/USDT" or "BTCUSDT".',
        },
        timeframe: {
          type: "string",
          description:
            'Candle width. Common values: "1m" "5m" "15m" "30m" "1h" "4h" "1d". ' +
            'Default "1h".',
          default: "1h",
        },
        limit: {
          type: "number",
          description: "Number of candles to return (1 – 1000). Default 100.",
          default: 100,
        },
      },
      required: ["symbol"],
    },
  },
  {
    name: "place_buy_order",
    description:
      "Place a spot market BUY order. Spot only — no leverage. " +
      "Requires API key with Trade permission.",
    inputSchema: {
      type: "object",
      properties: {
        symbol: {
          type: "string",
          description: 'Trading pair, e.g. "BTC/USDT" or "BTCUSDT".',
        },
        amount: {
          type: "number",
          description:
            "Base-asset quantity to buy (e.g. BTC amount for BTC/USDT).",
        },
      },
      required: ["symbol", "amount"],
    },
  },
  {
    name: "place_sell_order",
    description:
      "Place a spot market SELL order. Spot only — no leverage. " +
      "Requires API key with Trade permission.",
    inputSchema: {
      type: "object",
      properties: {
        symbol: {
          type: "string",
          description: 'Trading pair, e.g. "BTC/USDT" or "BTCUSDT".',
        },
        amount: {
          type: "number",
          description:
            "Base-asset quantity to sell (e.g. BTC amount for BTC/USDT).",
        },
      },
      required: ["symbol", "amount"],
    },
  },
  {
    name: "get_open_orders",
    description:
      "List all open orders, optionally filtered to a single symbol. " +
      "Requires API key with Read permission.",
    inputSchema: {
      type: "object",
      properties: {
        symbol: {
          type: "string",
          description:
            'Trading pair to filter, e.g. "BTC/USDT". ' +
            "Leave empty to list open orders across all pairs.",
        },
      },
      required: [],
    },
  },
  {
    name: "cancel_order",
    description:
      "Cancel an open order by its ID. " +
      "Requires API key with Trade permission.",
    inputSchema: {
      type: "object",
      properties: {
        order_id: {
          type: "string",
          description: "Order ID returned by place_buy_order / place_sell_order.",
        },
        symbol: {
          type: "string",
          description:
            'Trading pair the order was placed on, e.g. "BTC/USDT" or "BTCUSDT".',
        },
      },
      required: ["order_id", "symbol"],
    },
  },
];

// ── Tool handlers ─────────────────────────────────────────────────────────────

async function handle_get_balance() {
  requireCreds();
  await ensureMarkets();

  const balances = await exchange.fetchBalance({ type: "spot" });
  const usdt = balances["USDT"] ?? { free: 0, used: 0, total: 0 };

  return ok({
    asset:  "USDT",
    free:   usdt.free  ?? 0,
    locked: usdt.used  ?? 0,
    total:  usdt.total ?? 0,
  });
}

async function handle_get_ticker({ symbol }) {
  await ensureMarkets();
  const sym    = normalizeSymbol(symbol);
  const ticker = await exchange.fetchTicker(sym);

  return ok({
    symbol:           ticker.symbol,
    last_price:       ticker.last,
    bid:              ticker.bid,
    ask:              ticker.ask,
    high_24h:         ticker.high,
    low_24h:          ticker.low,
    volume_24h:       ticker.baseVolume,
    quote_volume_24h: ticker.quoteVolume,
    price_change_pct: ticker.percentage,
    timestamp:        ticker.datetime,
  });
}

async function handle_get_ohlcv({ symbol, timeframe = "1h", limit = 100 }) {
  await ensureMarkets();
  const sym  = normalizeSymbol(symbol);
  const cap  = Math.min(Math.max(1, Math.floor(limit)), 1000);
  const raw  = await exchange.fetchOHLCV(sym, timeframe, undefined, cap);

  const candles = raw.map(([ts, open, high, low, close, volume]) => ({
    time:   new Date(ts).toISOString(),
    open,
    high,
    low,
    close,
    volume,
  }));

  return ok({
    symbol:    sym,
    timeframe,
    count:     candles.length,
    candles,
  });
}

async function handle_place_buy_order({ symbol, amount }) {
  requireCreds();
  await ensureMarkets();

  if (amount <= 0) throw new Error("amount must be greater than 0.");

  const sym   = normalizeSymbol(symbol);
  const order = await exchange.createMarketBuyOrder(sym, amount);

  return ok(formatOrder(order));
}

async function handle_place_sell_order({ symbol, amount }) {
  requireCreds();
  await ensureMarkets();

  if (amount <= 0) throw new Error("amount must be greater than 0.");

  const sym   = normalizeSymbol(symbol);
  const order = await exchange.createMarketSellOrder(sym, amount);

  return ok(formatOrder(order));
}

async function handle_get_open_orders({ symbol } = {}) {
  requireCreds();
  await ensureMarkets();

  const sym    = symbol ? normalizeSymbol(symbol) : undefined;
  const orders = await exchange.fetchOpenOrders(sym);

  return ok({
    count:  orders.length,
    orders: orders.map(formatOrder),
  });
}

async function handle_cancel_order({ order_id, symbol }) {
  requireCreds();
  await ensureMarkets();

  const sym    = normalizeSymbol(symbol);
  const result = await exchange.cancelOrder(order_id, sym);

  return ok(formatOrder(result));
}

function formatOrder(o) {
  return {
    order_id:   o.id,
    symbol:     o.symbol,
    side:       o.side,
    type:       o.type,
    status:     o.status,
    amount:     o.amount,
    filled:     o.filled,
    remaining:  o.remaining,
    price:      o.price,
    average:    o.average,
    cost:       o.cost,
    placed_at:  o.datetime,
  };
}

// ── MCP server wiring ─────────────────────────────────────────────────────────

const server = new Server(
  { name: "mexc-trading", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args = {} } = request.params;

  try {
    switch (name) {
      case "get_balance":       return await handle_get_balance();
      case "get_ticker":        return await handle_get_ticker(args);
      case "get_ohlcv":         return await handle_get_ohlcv(args);
      case "place_buy_order":   return await handle_place_buy_order(args);
      case "place_sell_order":  return await handle_place_sell_order(args);
      case "get_open_orders":   return await handle_get_open_orders(args);
      case "cancel_order":      return await handle_cancel_order(args);
      default:
        return err(`Unknown tool: ${name}`);
    }
  } catch (e) {
    return err(e.message ?? String(e));
  }
});

// ── Start ─────────────────────────────────────────────────────────────────────

const transport = new StdioServerTransport();

if (!credsOk()) {
  process.stderr.write(
    "[mexc-mcp] WARNING: MEXC_API_KEY / MEXC_SECRET not set — " +
    "public tools (ticker, ohlcv) will work; private tools will return credential errors.\n"
  );
}

await server.connect(transport);
