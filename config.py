import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# MEXC credentials — set MEXC_API_KEY and MEXC_SECRET in .env
API_KEY:    str = os.getenv("MEXC_API_KEY", "")
API_SECRET: str = (os.getenv("MEXC_SECRET") or os.getenv("MEXC_API_SECRET", ""))  # .env may use either name

# Coinranking Professional API key — override via COINRANKING_API_KEY in .env
COINRANKING_API_KEY: str = os.getenv(
    "COINRANKING_API_KEY",
    "coinranking7dea2dbc3f9aa62ac8ff38f1ff3a6542f97670a8ee02e776",
)

BASE_URL = "https://api.mexc.com"
RECV_WINDOW = 5000

QUOTE_CURRENCY = "USDT"

# Dynamic pair selection (Coinranking Professional)
COIN_SELECTOR_REFRESH_HOURS = 4    # Rebuild pair universe every N hours
COINRANKING_PAGES = 2              # 2 × 100 = 200 coins per refresh
MIN_SELECTED_PAIRS = 5             # Supplement with fallbacks if fewer qualify
MAX_SELECTED_PAIRS = 50            # Hard cap on active trading pairs

# Coin quality gate — Coinranking 10-point scoring system
MIN_COIN_SCORE       = 7           # Coins scoring < 7/10 are never traded
MIN_MARKET_CAP_USD   = 500_000_000 # Minimum market cap: $500M
MIN_MEXC_24H_VOLUME_USD = 50_000_000  # Minimum 24h USDT volume on MEXC: $50M
MAX_CHANGE_PCT       = 20.0        # Maximum |24h change %| before a coin is skipped

# Used when Coinranking or MEXC APIs are unreachable during a refresh
FALLBACK_PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
]

# Candle timeframe
# MEXC v3 valid intervals: 1m 5m 15m 30m 60m 4h 1d 1W 1M
# MEXC uses "60m" — sending "1h" returns error -1121 (invalid interval).
PRIMARY_TIMEFRAME = "60m"
CANDLE_LIMIT = 200

# Strategy parameters
FVG_MIN_SIZE_PCT = 0.0010   # 0.10% minimum gap size to qualify as FVG
OB_MIN_IMPULSE_PCT = 0.0050 # 0.50% minimum impulse move to qualify as OB
OB_LOOKBACK = 30            # Candles to scan back when detecting order blocks
ATR_PERIOD = 14
ATR_SL_MULT = 1.5           # Stop loss = OB/FVG edge - ATR * multiplier
MIN_SIGNAL_STRENGTH = 0.25  # Reject signals weaker than this (0–1 scale)
TAKE_PROFIT_RR = 2.0        # Risk-reward ratio for take profit

# Risk management
MAX_RISK_PER_TRADE_PCT      = 0.01   # 1% of account balance at risk per trade
DAILY_LOSS_CAP_PCT          = 0.03   # Halt new entries after 3% intraday loss
WEEKLY_LOSS_CAP_PCT         = 0.08   # Halt new entries after 8% weekly loss
MAX_OPEN_POSITIONS          = 2      # Max simultaneous open positions
MAX_DAILY_TRADES            = 4      # Max new entries per calendar day
MAX_POSITION_PCT_OF_BALANCE = 0.10   # Hard cap: never spend > 10% on one trade

# Loop timing
LOOP_INTERVAL_SECONDS = 60  # Main loop cadence in seconds

# Market context (Coinranking /v2/stats polling)
MARKET_CONTEXT_REFRESH_MIN = 30    # Poll /v2/stats every N minutes
BTC_DOM_ALTCOIN_RESTRICT   = 55.0  # BTC dominance % above which altcoin entries are reduced
MARKET_DROP_NO_TRADE_PCT   = -3.0  # Global 24h mcap change below which no new entries open

# HTF bias filter
HTF_TIMEFRAME    = "4h"
HTF_CANDLE_LIMIT = 50

# Session filter (UTC hours)
SESSION_FILTER_ENABLED = True
LONDON_OPEN  = 7
LONDON_CLOSE = 12
NY_OPEN      = 13
NY_CLOSE     = 17

# Trailing stop / break-even
BREAKEVEN_R    = 1.0   # Move SL to entry when price reaches entry + 1× risk
TRAIL_START_R  = 1.5   # Start trailing when price reaches entry + 1.5× risk
ATR_TRAIL_MULT = 1.0   # Trail distance = ATR × this multiplier
