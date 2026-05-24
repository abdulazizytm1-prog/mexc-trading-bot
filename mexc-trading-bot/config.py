import os
from dotenv import load_dotenv

load_dotenv()

API_KEY: str = os.getenv("MEXC_API_KEY", "")
API_SECRET: str = os.getenv("MEXC_API_SECRET", "")

BASE_URL = "https://api.mexc.com"

APPROVED_COINS = ["BTC", "ETH", "SOL", "ADA", "AVAX"]
QUOTE_ASSET = "USDT"
TRADING_PAIRS = [f"{coin}{QUOTE_ASSET}" for coin in APPROVED_COINS]

# Risk settings
RISK_PER_TRADE_PCT = 0.01      # Risk 1% of balance per trade
MAX_POSITION_PCT = 0.05        # Cap position at 5% of balance
DAILY_LOSS_CAP_PCT = 0.02      # Halt trading when daily loss exceeds 2%

# Strategy settings
KLINE_INTERVAL = "1h"
KLINE_LIMIT = 200
FVG_MIN_SIZE_PCT = 0.002       # Minimum FVG width as a fraction of price (0.2%)
OB_LOOKBACK = 20               # Max order blocks to keep per symbol
TP_RR_RATIO = 2.0              # Take-profit reward:risk ratio
SL_BUFFER = 0.005              # 0.5% buffer below FVG/OB for stop-loss

POLL_INTERVAL = 60             # Seconds between main loop iterations
REQUEST_TIMEOUT = 10           # HTTP request timeout in seconds
