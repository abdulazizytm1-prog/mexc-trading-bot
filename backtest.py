"""
backtest.py — Walk-forward backtesting engine for the MEXC SMC trading bot.

Walk-forward rules (zero lookahead bias):
  - Signal generated from df[0..i] (candle i just closed)
  - Entry at candle[i+1].open  (next candle open)
  - Exits checked on each subsequent candle's OHLC
  - Candle direction heuristic for same-candle TP/SL conflict:
      bullish candle → low comes first → SL wins
      bearish candle → high comes first → TP wins
  - TP1 = 1R close 50% | TP2 = 2R close 25% | TP3 = 3R close 25% | SL full close
  - Sizing: 1% of rolling equity per trade (compound)
  - Kill zone clock patched to historical candle timestamp

Usage:
    python backtest.py --symbol BTCUSDT --days 90 --score 7
    python backtest.py --matrix                     # full 3×3 test grid
    python backtest.py --symbol SOLUSDT --refresh   # force re-download
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

# ── Project root on path ────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

import strategy as _strat                      # patched below
from config import ATR_PERIOD, OB_LOOKBACK
from mexc_api import MEXCSpotAPI, MEXCAPIError
from strategy import candles_to_df, generate_signal

# ── Silence noisy loggers during simulation ─────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(message)s")
for _noisy in ("strategy", "market_context", "risk_manager", "mexc_api.errors"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
DATA_DIR         = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

_INITIAL_EQUITY   = 10_000.0   # USD
_RISK_PCT         = 0.01       # 1% per trade
_HOURS_PER_DAY    = 24
_SIG_WINDOW       = 200        # 1H candles fed to generate_signal (matches CANDLE_LIMIT)
_HTF_WINDOW       = 50         # 4H candles passed as htf_df
_MIN_CANDLES      = max(ATR_PERIOD + 5, OB_LOOKBACK + 5, 50)
_PAGE_SIZE        = 1000       # MEXC klines limit per call
_SL_CAP_PCT       = 0.03       # hard SL cap: never > 3% from entry

MATRIX_SYMBOLS    = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
MATRIX_SCORES     = [6, 7, 8]
MATRIX_DAYS       = 90


# ══════════════════════════════════════════════════════════════════════════════
#  Kill-zone time patch
#  generate_signal() calls detect_kill_zone() with no args → uses system clock.
#  We replace it with a version that uses the current backtest candle's time.
# ══════════════════════════════════════════════════════════════════════════════

_BACKTEST_DT: Optional[datetime] = None
_orig_kz = _strat.detect_kill_zone


def _bt_detect_kill_zone(dt: Optional[datetime] = None) -> Optional[str]:
    return _orig_kz(dt or _BACKTEST_DT)


_strat.detect_kill_zone = _bt_detect_kill_zone


# ══════════════════════════════════════════════════════════════════════════════
#  Data structures
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    symbol:      str
    entry_idx:   int
    entry_time:  str
    entry_price: float
    sl:          float
    tp1:         float
    tp2:         float
    tp3:         float
    score:       float
    risk:        float
    exit_idx:    int   = 0
    exit_time:   str   = ""
    exit_type:   str   = ""    # SL | TP1_only | TP2 | TP3 | END
    pnl_r:       float = 0.0   # combined R-multiple (accounting for partials)
    duration_h:  int   = 0


@dataclass
class BacktestResult:
    symbol:        str
    days:          int
    min_score:     float
    total_trades:  int   = 0
    wins:          int   = 0
    losses:        int   = 0
    winrate:       float = 0.0
    profit_factor: float = 0.0
    avg_rr:        float = 0.0
    total_pnl_r:   float = 0.0
    max_drawdown:  float = 0.0
    trades:        List[Trade] = field(default_factory=list)
    equity_curve:  List[float] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
#  Data layer — fetch + cache
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_and_cache(
    api: MEXCSpotAPI,
    symbol: str,
    days: int,
    refresh: bool = False,
) -> pd.DataFrame:
    """Return a 1H OHLCV DataFrame for `symbol`, covering at least `days` + buffer."""
    cache = DATA_DIR / f"{symbol}_1h.csv"
    needed = days * _HOURS_PER_DAY + _MIN_CANDLES + 10

    # ── Use cache when it has enough history and is fresh ────────────────────
    if not refresh and cache.exists():
        df = pd.read_csv(cache)
        df["open_time"] = pd.to_datetime(df["open_time"], utc=False)
        if df["open_time"].dt.tz is not None:
            df["open_time"] = df["open_time"].dt.tz_localize(None)
        now_naive    = pd.Timestamp.now("UTC").tz_localize(None)
        oldest       = df["open_time"].min()
        needed_start = now_naive - pd.Timedelta(days=days + 2)
        fresh        = (now_naive - df["open_time"].max()).total_seconds() < 7200
        if oldest <= needed_start and fresh:
            _echo(f"  [{symbol}] {len(df):,} candles from cache ({cache.name})")
            return df

    # ── Download from MEXC — paginate with startTime + endTime window ────────
    # Strategy A: pass both startTime and endTime to define an explicit
    # 500-candle window and slide it forward.  If MEXC returns candles outside
    # our window (both params ignored), fall back to Strategy B.
    # Strategy B: single latest-N fetch (MEXC fallback, fewer candles).
    _echo(f"  [{symbol}] downloading {needed} candles from MEXC ...")
    now_ms:        int  = int(time.time() * 1000)
    page_ms:       int  = _PAGE_SIZE * 3_600_000       # window per page (500 h)
    current_start: int  = now_ms - needed * 3_600_000  # earliest we need
    all_klines:    list = []
    pages_tried:   int  = 0

    while len(all_klines) < needed and current_start < now_ms:
        page_end = min(current_start + page_ms, now_ms)
        try:
            chunk = api._get("/api/v3/klines", {
                "symbol":    symbol,
                "interval":  "60m",
                "startTime": current_start,
                "endTime":   page_end,
                "limit":     _PAGE_SIZE,
            })
        except Exception as exc:
            _echo(f"  [{symbol}] time-range params failed ({exc}); using latest-N ...")
            chunk = None

        pages_tried += 1

        # ── Detect whether MEXC honoured the window ───────────────────────
        if chunk:
            first_ms = int(chunk[0][0])
            last_ms  = int(chunk[-1][0])
            window_ok = (first_ms >= current_start - 3_600_000 and
                         last_ms  <= page_end   + 3_600_000)

            if window_ok:
                all_klines.extend(chunk)
                if pages_tried == 1:
                    _echo(f"  [{symbol}] time-window pagination active ...")
                current_start = last_ms + 3_600_000
                if not chunk or last_ms >= now_ms - 3_600_000:
                    break
                continue                         # try next page

            # MEXC returned data outside our window → params are ignored
            if pages_tried == 1:
                _echo(f"  [{symbol}] time params not honoured; "
                      f"using latest {len(chunk)} candles only.")
            all_klines = chunk
            break

        # chunk is None or empty — params unsupported, try plain fetch
        try:
            chunk = api.get_klines(symbol, "60m", _PAGE_SIZE)
        except Exception as exc2:
            print(f"  [{symbol}] ERROR: {exc2}", file=sys.stderr)
            return pd.DataFrame()
        all_klines = chunk or []
        _echo(f"  [{symbol}] plain fetch returned {len(all_klines)} candles.")
        break

    if not all_klines:
        print(f"  [{symbol}] ERROR: MEXC returned no data", file=sys.stderr)
        return pd.DataFrame()

    df = candles_to_df(all_klines)
    if df.empty:
        return df

    df = (df
          .drop_duplicates(subset=["open_time"])
          .sort_values("open_time")
          .reset_index(drop=True))

    df.to_csv(cache, index=False)
    _echo(f"  [{symbol}] saved {len(df):,} candles -> {cache.name}")
    return df


def _resample_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Down-sample a 1H DataFrame to 4H OHLCV candles."""
    if df.empty:
        return pd.DataFrame()
    try:
        df_idx = df.set_index("open_time")
        if df_idx.index.tz is not None:
            df_idx.index = df_idx.index.tz_localize(None)
        df_4h = df_idx.resample("4h").agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            quote_volume=("quote_volume", "sum"),
        ).dropna(subset=["open", "close"])
        df_4h = df_4h.reset_index()                # open_time becomes column
        # reset_index() can produce a tz-aware DatetimeIndex-turned-column;
        # strip tz so comparisons with the tz-naive 1H open_time don't crash.
        df_4h["open_time"] = pd.to_datetime(df_4h["open_time"]).dt.tz_localize(None)
        df_4h["close_time"] = df_4h["open_time"]   # placeholder for candles_to_df compat
        return df_4h.reset_index(drop=True)
    except Exception as exc:
        log.warning("4H resample failed: %s", exc)
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
#  Exit simulation — one candle at a time
# ══════════════════════════════════════════════════════════════════════════════

def _simulate_candle(candle: pd.Series, pos: dict) -> Tuple[bool, str, float]:
    """
    Evaluate one closed candle against an open position.
    Mutates pos for partial TP fills.
    Returns (fully_closed: bool, exit_type: str, pnl_r: float).

    Same-candle TP/SL conflict heuristic:
      Bullish candle → low before high → SL wins over TP on first contact.
      Bearish candle → high before low → TP wins over SL on first contact.
    """
    lo   = float(candle["low"])
    hi   = float(candle["high"])
    cl   = float(candle["close"])
    op   = float(candle["open"])
    bull = cl >= op

    # ── Partial-close helpers ─────────────────────────────────────────────────
    def _hit_tp1() -> None:
        if not pos["tp1_hit"]:
            pos["partial_r"] += 0.50 * 1.0     # 50% closed at 1R
            pos["remaining"]  = 0.50
            pos["tp1_hit"]    = True

    def _hit_tp2() -> None:
        if pos["tp1_hit"] and not pos["tp2_hit"]:
            pos["partial_r"] += 0.25 * 2.0     # 25% closed at 2R
            pos["remaining"]  = 0.25
            pos["tp2_hit"]    = True

    def _sl_pnl() -> float:
        return pos["partial_r"] + pos["remaining"] * (-1.0)

    def _tp3_pnl() -> float:
        return pos["partial_r"] + pos["remaining"] * 3.0   # remaining 25% at 3R

    # ── Bullish candle: low → high (SL fires before TPs on same candle) ──────
    if bull:
        if lo <= pos["sl"] and not pos["tp1_hit"]:
            return True, "SL", _sl_pnl()
        if hi >= pos["tp1"]:
            _hit_tp1()
        if hi >= pos["tp2"]:
            _hit_tp2()
        if pos["tp2_hit"] and hi >= pos["tp3"]:
            return True, "TP3", _tp3_pnl()
        if lo <= pos["sl"]:                         # SL after partial TPs
            return True, "SL", _sl_pnl()

    # ── Bearish candle: high → low (TPs fire before SL on same candle) ───────
    else:
        if hi >= pos["tp1"]:
            _hit_tp1()
        if hi >= pos["tp2"]:
            _hit_tp2()
        if pos["tp2_hit"] and hi >= pos["tp3"]:
            return True, "TP3", _tp3_pnl()
        if lo <= pos["sl"]:
            return True, "SL", _sl_pnl()

    return False, "", 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  Core walk-forward engine
# ══════════════════════════════════════════════════════════════════════════════

def _run_backtest(
    df:        pd.DataFrame,
    symbol:    str,
    days:      int,
    min_score: float,
) -> BacktestResult:
    global _BACKTEST_DT

    result = BacktestResult(symbol=symbol, days=days, min_score=min_score)

    # ── Trim to requested window (keep extra lookback at front) ──────────────
    cutoff_ts  = df["open_time"].max() - pd.Timedelta(days=days)
    start_idx  = df.index[df["open_time"] >= cutoff_ts].min()
    if pd.isna(start_idx):
        start_idx = 0
    # Walk-forward starts after the lookback buffer
    walk_start = max(int(start_idx), _MIN_CANDLES)

    n            = len(df)
    df_4h_full   = _resample_4h(df)

    # Set _MIN_SCORE to 0 so generate_signal returns all signals;
    # we filter by min_score ourselves for clean metric separation.
    _saved_min   = _strat._MIN_SCORE
    _strat._MIN_SCORE = 0

    pos:            Optional[dict]   = None    # active position state
    pending_signal: Optional[object] = None    # TradeSignal queued for next-open entry
    equity         = _INITIAL_EQUITY
    result.equity_curve.append(equity)

    total_steps = n - 1 - walk_start
    done        = 0

    try:
        for i in range(walk_start, n - 1):
            candle = df.iloc[i]
            ts     = candle["open_time"]

            # ── Update backtest clock ─────────────────────────────────────────
            if isinstance(ts, pd.Timestamp):
                _BACKTEST_DT = ts.to_pydatetime()
                if _BACKTEST_DT.tzinfo is None:
                    _BACKTEST_DT = _BACKTEST_DT.replace(tzinfo=timezone.utc)

            # ── A: Execute pending entry at this candle's open ────────────────
            if pending_signal is not None and pos is None:
                sig         = pending_signal
                entry_price = float(candle["open"])
                raw_risk    = entry_price - sig.stop_loss

                if raw_risk > 0:
                    sl    = max(sig.stop_loss, entry_price * (1.0 - _SL_CAP_PCT))
                    risk  = entry_price - sl

                    if risk > 0:
                        tp1 = entry_price + risk * 1.0
                        tp2 = entry_price + risk * 2.0
                        tp3 = entry_price + risk * 3.0

                        pos = {
                            "entry_idx":   i,
                            "entry_time":  str(ts),
                            "entry_price": entry_price,
                            "sl":          sl,
                            "tp1":         tp1,
                            "tp2":         tp2,
                            "tp3":         tp3,
                            "risk":        risk,
                            "score":       sig.score,
                            "partial_r":   0.0,
                            "remaining":   1.0,
                            "tp1_hit":     False,
                            "tp2_hit":     False,
                        }

                pending_signal = None

            # ── B: Check exits on the current closed candle ───────────────────
            if pos is not None:
                closed, exit_type, pnl_r = _simulate_candle(candle, pos)

                if closed:
                    risk_usdt = equity * _RISK_PCT
                    equity   += pnl_r * risk_usdt

                    t = Trade(
                        symbol      = symbol,
                        entry_idx   = pos["entry_idx"],
                        entry_time  = pos["entry_time"],
                        entry_price = pos["entry_price"],
                        sl          = pos["sl"],
                        tp1         = pos["tp1"],
                        tp2         = pos["tp2"],
                        tp3         = pos["tp3"],
                        score       = pos["score"],
                        risk        = pos["risk"],
                        exit_idx    = i,
                        exit_time   = str(ts),
                        exit_type   = exit_type,
                        pnl_r       = round(pnl_r, 4),
                        duration_h  = i - pos["entry_idx"],
                    )
                    result.trades.append(t)
                    result.equity_curve.append(equity)
                    pos = None

            # ── C: Generate signal when flat ──────────────────────────────────
            if pos is None and pending_signal is None:
                sl_start   = max(0, i - _SIG_WINDOW + 1)
                df_slice   = df.iloc[sl_start : i + 1].reset_index(drop=True)

                htf_df = None
                if not df_4h_full.empty:
                    htf_mask = df_4h_full["open_time"] < ts
                    htf_sub  = df_4h_full[htf_mask].iloc[-_HTF_WINDOW:].reset_index(drop=True)
                    if len(htf_sub) >= 10:
                        htf_df = htf_sub

                try:
                    sig = generate_signal(
                        symbol, df_slice,
                        htf_df=htf_df,
                        trade_type="daytrading",
                    )
                except Exception:
                    sig = None

                if sig is not None and sig.score >= min_score:
                    pending_signal = sig

            done += 1
            if done % 300 == 0:
                pct = done / max(total_steps, 1) * 100
                _echo(
                    f"  [{symbol}] score>={min_score:.0f}  "
                    f"{pct:4.0f}%  ({done}/{total_steps})  "
                    f"trades={len(result.trades)}",
                    end="\r",
                )

    finally:
        _strat._MIN_SCORE = _saved_min

    _echo("")   # newline after \r progress

    # ── Close any position still open at end of data ─────────────────────────
    if pos is not None:
        last  = df.iloc[-1]
        cl    = float(last["close"])
        risk  = pos["risk"]
        close_r = (cl - pos["entry_price"]) / risk if risk > 0 else 0.0
        pnl_r   = pos["partial_r"] + pos["remaining"] * close_r
        equity += pnl_r * equity * _RISK_PCT

        result.trades.append(Trade(
            symbol      = symbol,
            entry_idx   = pos["entry_idx"],
            entry_time  = pos["entry_time"],
            entry_price = pos["entry_price"],
            sl          = pos["sl"],
            tp1         = pos["tp1"],
            tp2         = pos["tp2"],
            tp3         = pos["tp3"],
            score       = pos["score"],
            risk        = pos["risk"],
            exit_idx    = n - 1,
            exit_time   = str(df.iloc[-1]["open_time"]),
            exit_type   = "END",
            pnl_r       = round(pnl_r, 4),
            duration_h  = n - 1 - pos["entry_idx"],
        ))
        result.equity_curve.append(equity)

    # ── Metrics ───────────────────────────────────────────────────────────────
    trades = result.trades
    result.total_trades = len(trades)

    if not trades:
        return result

    wins   = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r <= 0]
    result.wins   = len(wins)
    result.losses = len(losses)

    gross_profit  = sum(t.pnl_r for t in wins)
    gross_loss    = abs(sum(t.pnl_r for t in losses))
    total_pnl_r   = sum(t.pnl_r for t in trades)

    result.winrate       = round(result.wins / result.total_trades * 100, 1)
    result.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 9.99
    result.avg_rr        = round(total_pnl_r / result.total_trades, 3)
    result.total_pnl_r   = round(total_pnl_r, 3)

    # Max drawdown from equity curve
    curve  = result.equity_curve
    peak   = curve[0]
    max_dd = 0.0
    for eq in curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    result.max_drawdown = round(max_dd, 2)

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  Output helpers
# ══════════════════════════════════════════════════════════════════════════════

def _echo(msg: str, end: str = "\n") -> None:
    print(msg, end=end, flush=True)


def _print_result(r: BacktestResult) -> None:
    final_eq = r.equity_curve[-1] if r.equity_curve else _INITIAL_EQUITY
    pnl_pct  = (final_eq - _INITIAL_EQUITY) / _INITIAL_EQUITY * 100
    bar      = "-" * 58

    _echo(f"\n{bar}")
    _echo(f"  {r.symbol}  |  {r.days}d  |  MIN_SCORE >= {r.min_score:.0f}")
    _echo(bar)
    _echo(f"  Total trades    : {r.total_trades}")
    _echo(f"  Wins / Losses   : {r.wins} W  /  {r.losses} L")
    _echo(f"  Win rate        : {r.winrate:.1f}%")
    _echo(f"  Profit factor   : {r.profit_factor:.2f}")
    _echo(f"  Avg R / trade   : {r.avg_rr:+.3f}R")
    _echo(f"  Total P&L       : {r.total_pnl_r:+.2f}R")
    _echo(f"  Max drawdown    : {r.max_drawdown:.2f}%")
    _echo(f"  Final equity    : ${final_eq:,.2f}  ({pnl_pct:+.1f}%)")
    _echo(bar)


def _print_matrix(results: List[BacktestResult]) -> None:
    bar = "=" * 66
    _echo(f"\n{bar}")
    _echo("  WALK-FORWARD BACKTEST MATRIX")
    _echo(bar)
    _echo(f"  {'Symbol':<10} {'Score':>5} {'Trades':>6} {'WR%':>6} "
          f"{'PF':>5} {'AvgR':>7} {'TotalR':>7} {'MaxDD%':>7}")
    _echo(f"  {'-' * 62}")
    for r in results:
        _echo(
            f"  {r.symbol:<10} {r.min_score:>5.0f} {r.total_trades:>6} "
            f"{r.winrate:>6.1f} {r.profit_factor:>5.2f} "
            f"{r.avg_rr:>+7.3f} {r.total_pnl_r:>+7.2f} {r.max_drawdown:>7.2f}"
        )
    _echo(bar)


def _save_equity_csv(r: BacktestResult, path: Path) -> Path:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trade_num", "equity_usdt", "pnl_pct"])
        for i, eq in enumerate(r.equity_curve):
            pnl_pct = (eq - _INITIAL_EQUITY) / _INITIAL_EQUITY * 100
            w.writerow([i, round(eq, 2), round(pnl_pct, 3)])
    return path


def _save_trades_csv(r: BacktestResult, path: Path) -> Path:
    if not r.trades:
        return path
    fields = [
        "symbol", "entry_time", "exit_time", "entry_price", "sl",
        "tp1", "tp2", "tp3", "exit_type", "pnl_r", "score", "duration_h",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in r.trades:
            w.writerow({
                "symbol":      t.symbol,
                "entry_time":  t.entry_time,
                "exit_time":   t.exit_time,
                "entry_price": round(t.entry_price, 8),
                "sl":          round(t.sl, 8),
                "tp1":         round(t.tp1, 8),
                "tp2":         round(t.tp2, 8),
                "tp3":         round(t.tp3, 8),
                "exit_type":   t.exit_type,
                "pnl_r":       t.pnl_r,
                "score":       t.score,
                "duration_h":  t.duration_h,
            })
    return path


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Walk-forward backtest for the MEXC SMC trading bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python backtest.py --symbol BTCUSDT --days 90 --score 7\n"
            "  python backtest.py --matrix\n"
            "  python backtest.py --symbol ETHUSDT --days 60 --score 6 --refresh"
        ),
    )
    parser.add_argument("--symbol",  default="BTCUSDT",
                        help="MEXC symbol (default: BTCUSDT)")
    parser.add_argument("--days",    type=int,   default=90,
                        help="History window in days (default: 90)")
    parser.add_argument("--score",   type=float, default=7.0,
                        help="Minimum signal score gate (default: 7)")
    parser.add_argument("--matrix",  action="store_true",
                        help="Run full 3×3 test matrix")
    parser.add_argument("--refresh", action="store_true",
                        help="Force re-download ignoring CSV cache")
    parser.add_argument("--no-save", action="store_true",
                        help="Skip saving CSV output files")
    args = parser.parse_args()

    api = MEXCSpotAPI()

    if args.matrix:
        _echo(f"\nMatrix: {MATRIX_SYMBOLS} x scores {MATRIX_SCORES} x {MATRIX_DAYS}d")
        all_results: List[BacktestResult] = []

        for sym in MATRIX_SYMBOLS:
            _echo(f"\n{'-'*40}")
            df = _fetch_and_cache(api, sym, MATRIX_DAYS, args.refresh)
            if df.empty:
                _echo(f"  [{sym}] no data - skipping")
                continue
            for sc in MATRIX_SCORES:
                _echo(f"\n  [{sym}] score>={sc}  ({MATRIX_DAYS}d) ...")
                r = _run_backtest(df, sym, MATRIX_DAYS, sc)
                all_results.append(r)

                if not args.no_save:
                    eq_path = DATA_DIR / f"equity_{sym}_s{int(sc)}_{MATRIX_DAYS}d.csv"
                    tr_path = DATA_DIR / f"trades_{sym}_s{int(sc)}_{MATRIX_DAYS}d.csv"
                    _save_equity_csv(r, eq_path)
                    _save_trades_csv(r, tr_path)
                    _echo(f"  -> equity: {eq_path.name}  trades: {tr_path.name}")

        _print_matrix(all_results)

    else:
        _echo(f"\nBacktesting {args.symbol}  |  {args.days}d  |  score>={args.score}")
        df = _fetch_and_cache(api, args.symbol, args.days, args.refresh)
        if df.empty:
            _echo("No data returned — check MEXC credentials and symbol name.")
            sys.exit(1)

        r = _run_backtest(df, args.symbol, args.days, args.score)
        _print_result(r)

        if not args.no_save:
            eq_path = Path("equity_curve.csv")
            tr_path = Path(f"trades_{args.symbol}_{args.days}d_s{int(args.score)}.csv")
            _save_equity_csv(r, eq_path)
            _save_trades_csv(r, tr_path)
            _echo(f"\n  Equity curve -> {eq_path}")
            _echo(f"  Trade log    -> {tr_path}")


if __name__ == "__main__":
    main()
