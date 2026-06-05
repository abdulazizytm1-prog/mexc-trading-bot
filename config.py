import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# MEXC credentials — set MEXC_API_KEY and MEXC_SECRET in .env
API_KEY:    str = os.getenv("MEXC_API_KEY", "")
API_SECRET: str = (os.getenv("MEXC_SECRET") or os.getenv("MEXC_API_SECRET", ""))  # .env may use either name

# Coinranking Professional API key — override via COINRANKING_API_KEY in .env
COINRANKING_API_KEY: str = os.getenv("COINRANKING_API_KEY", "")

BASE_URL = "https://api.mexc.com"
RECV_WINDOW = 5000

QUOTE_CURRENCY = "USDT"

# Dynamic pair selection (Coinranking Professional)
COIN_SELECTOR_REFRESH_HOURS = 4    # Rebuild pair universe every N hours
COINRANKING_PAGES = 3              # 3 × 100 = 300 coins per refresh
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

# ─────────────────────────────────────────────────────────────────────────── #
#  SMC / ICT Framework — Named Parameters (v2, all backtest-tunable)          #
# ─────────────────────────────────────────────────────────────────────────── #

# ── Market Structure ─────────────────────────────────────────────────────── #
SWING_LOOKBACK          = 5       # candles each side to confirm a swing point
MIN_SWING_DISTANCE_PCT  = 0.015   # 1.5 % min price distance between consecutive swings
BOS_CLOSE_BUFFER_PCT    = 0.001   # close must exceed level by 0.1 % to count as BOS
FALSE_BREAK_CANDLES     = 2       # BOS void if price reverses within N candles
MAX_SWING_AGE_CANDLES   = 100     # swings older than N × 1H candles are discarded

# ── Liquidity / Sweep ────────────────────────────────────────────────────── #
SWEEP_WICK_MIN_PCT      = 0.0015  # wick must penetrate level by ≥ 0.15 %
SWEEP_WICK_RATIO        = 0.30    # wick must be ≥ 30 % of total candle range
SWEEP_VOLUME_MULT       = 1.3     # sweep candle volume > N × 20-bar avg
SWEEP_CONFIRM_CANDLES   = 3       # displacement must follow within N candles
EQUAL_LEVEL_TOLERANCE   = 0.001   # two lows within 0.1 % = equal-low pool
MAX_LIQUIDITY_AGE       = 120     # pool levels older than N × 1H candles expire
MIN_POOL_STRENGTH       = 0.40    # minimum pool strength score to generate a signal

# ── Fair Value Gap ───────────────────────────────────────────────────────── #
FVG_MIN_SIZE_ATR_MULT   = 0.30    # FVG height ≥ N × ATR_14 (or FVG_MIN_SIZE_PCT)
FVG_MAX_AGE_CANDLES     = 200     # FVGs older than N candles are discarded
FVG_CE_RATIO            = 0.50    # consequent encroachment at 50 % of FVG height
FVG_INVALIDATION_PCT    = 0.001   # body close N % below bottom = zone breached

# ── Order Block ──────────────────────────────────────────────────────────── #
OB_BODY_MIN_PCT         = 0.003   # 0.3 % min OB body size (filters dojis)
OB_DISP_CANDLES         = 3       # displacement must fire within N candles of OB
OB_MAX_AGE_CANDLES      = 150     # OBs older than N candles are discarded
OB_CE_RATIO             = 0.50    # OB consequent encroachment at 50 %
OB_INVALIDATION_PCT     = 0.001   # body close N % below wick low = breached

# ── Premium / Discount / OTE ─────────────────────────────────────────────── #
RANGE_MIN_SIZE_PCT      = 0.010   # 1.0 % min range height to be structurally valid
EQ_BUFFER_PCT           = 0.020   # ± 2 % around 50 % = "equilibrium" zone
OTE_FIB_LOW             = 0.618   # OTE zone top  (61.8 % retracement from high)
OTE_FIB_MID             = 0.705   # precise OTE   (70.5 % retracement — ICT level)
OTE_FIB_HIGH            = 0.786   # OTE zone bottom (78.6 % retracement)
OTE_FIB_EXTENDED        = 0.886   # extended OTE (last resort; confidence penalty)
OTE_MIN_IMPULSE_PCT     = 0.020   # 2.0 % min impulse size to define a valid OTE

# ── Displacement ─────────────────────────────────────────────────────────── #
DISP_BODY_MIN_PCT       = 0.006   # 0.6 % min body / close price
DISP_BODY_RANGE_RATIO   = 0.55    # body ≥ 55 % of total candle range
DISP_ATR_MULT           = 1.5     # candle range ≥ N × ATR_14
DISP_VOLUME_MULT        = 1.4     # volume ≥ N × 20-bar rolling avg
DISP_CLOSE_POSITION_MIN = 0.60    # close must be in upper 60 % of range
DISP_CANCEL_RATIO       = 0.70    # next-candle retracement > N % → cancelled
DISP_MIN_QUALITY        = 0.50    # minimum quality score to credit displacement

# ── Confirmation Candle ──────────────────────────────────────────────────── #
ENGULF_BODY_MULT        = 1.0     # engulf body ≥ N × prior bearish body
ENGULF_STRONG_MULT      = 1.5     # "strong engulf" threshold
RECLAIM_MAX_PCT         = 0.010   # violation must be < 1.0 % to qualify as reclaim
RECLAIM_MAX_CANDLES     = 4       # zone violation must be within N candles
CONFIRM_VOLUME_MULT     = 1.3     # confirmation candle volume > N × avg
MAX_ZONE_DWELL          = 8       # setup voided after price dwells N candles in zone
MSS_LOOKBACK            = 3       # pivot lookback for MSS (LTF CHoCH) detection

# ── Sessions / Kill Zones ────────────────────────────────────────────────── #
LOKZ_START              = 7.0     # London Open KZ start (UTC decimal hour)
LOKZ_CORE_START         = 8.0     # London Open core window start (UTC)
LOKZ_CORE_END           = 9.0     # London Open core window end (UTC)
LOKZ_END                = 10.0    # London Open KZ end (UTC)
NYOKZ_START             = 12.0    # NY Open KZ start (UTC)
NYOKZ_CORE_START        = 13.0    # NY Open core window start (UTC)
NYOKZ_CORE_END          = 14.0    # NY Open core window end (UTC)
NYOKZ_END               = 15.0    # NY Open KZ end (UTC)
LCKZ_START              = 14.0    # London Close KZ start (UTC)
LCKZ_END                = 16.5    # London Close KZ end (UTC)
KZ_CORE_BONUS           = 0.15    # confidence bonus inside core (1-hr) kill zone
KZ_OUTER_BONUS          = 0.08    # confidence bonus in outer kill zone
KZ_LCKZ_PENALTY         = 0.05    # London Close confidence penalty (retracement risk)
ASIAN_SESSION_PENALTY   = 0.10    # penalty for signals during Asian mid-session
DOW_MONDAY_BONUS        = 0.08    # Monday: fresh weekly liquidity bonus
DOW_FRIDAY_PENALTY      = 0.10    # Friday: early position-squaring penalty

# ── VWAP ─────────────────────────────────────────────────────────────────── #
VWAP_MIN_CANDLES        = 3       # min candles in session for valid VWAP
AVWAP_MIN_CANDLES       = 5       # min candles since anchor for valid AVWAP
VWAP_SLOPE_WINDOW       = 3       # candles used to classify VWAP slope
VWAP_SLOPE_RISING_PCT   = 0.0003  # ≥ 0.03 % per candle = "rising"
VWAP_ABOVE_BONUS        = 0.10    # confidence bonus when price > daily VWAP
VWAP_BELOW_PENALTY      = 0.12    # confidence penalty when price < daily VWAP
VWAP_SD2_LOWER_BONUS    = 0.12    # bonus at extreme discount (below SD2 lower)
VWAP_SD1_LOWER_BONUS    = 0.08    # bonus at standard discount (below SD1 lower)
VWAP_SD2_UPPER_PENALTY  = 0.15    # penalty at extreme premium (above SD2 upper)
AVWAP_CONFLUENCE_BONUS  = 0.12    # bonus when AVWAP aligns with OTE zone

# ── Signal Gate ──────────────────────────────────────────────────────────── #
MIN_SIGNAL_SCORE        = 6       # minimum confluence score (0 – 10) to pass strategy filter

# ── Execution Gate ────────────────────────────────────────────────────────── #
USE_CLAUDE_GATE         = False   # True  → require Anthropic API approval per signal
                                  # False → execute automatically when score ≥ EXECUTE_SCORE
EXECUTE_SCORE           = 6.0     # auto-execute threshold when USE_CLAUDE_GATE = False
