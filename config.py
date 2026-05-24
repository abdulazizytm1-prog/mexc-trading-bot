import os
from dotenv import load_dotenv

load_dotenv()

# API credentials — set these in .env
API_KEY: str = os.getenv("MEXC_API_KEY", "")
API_SECRET: str = os.getenv("MEXC_API_SECRET", "")

BASE_URL = "https://api.mexc.com"
RECV_WINDOW = 5000

QUOTE_CURRENCY = "USDT"

# Dynamic pair selection (replaces hardcoded list)
COIN_SELECTOR_REFRESH_HOURS = 4   # How often to re-score and rebuild the pair list
COINGECKO_PAGES = 2               # 2 × 250 = 500 coins fetched from CoinGecko
MIN_SELECTED_PAIRS = 5            # Supplement with fallback if fewer than this qualify
MAX_SELECTED_PAIRS = 20           # Maximum pairs in the active trading universe
MIN_MEXC_24H_VOLUME_USD = 500_000 # Drop any pair with < $500k 24h USDT volume on MEXC

# Used when CoinGecko or MEXC APIs are unreachable during a refresh
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
MAX_RISK_PER_TRADE_PCT = 0.01   # 1% of account balance at risk per trade
DAILY_LOSS_CAP_PCT = 0.05       # Stop trading for the day after 5% drawdown
MAX_OPEN_POSITIONS = 3          # Concurrent open positions cap
MAX_POSITION_PCT_OF_BALANCE = 0.10  # Hard cap: never spend > 10% on one trade

# Loop timing
LOOP_INTERVAL_SECONDS = 60  # Main loop cadence in seconds
