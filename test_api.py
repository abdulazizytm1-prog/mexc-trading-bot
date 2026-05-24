"""
Smoke-tests for the MEXC API wrapper and candles_to_df parser.

Run:
    python test_api.py

Does NOT place any orders.  No API keys needed for tests 1-6.
"""

import sys
from mexc_api import MEXCSpotAPI, MEXCAPIError
from strategy import candles_to_df, _MEXC_KLINE_COLS, _MEXC_KLINE_NCOLS


def _ok(label: str) -> None:
    print(f"  PASS  {label}")


def _fail(label: str, detail: str) -> None:
    print(f"  FAIL  {label}: {detail}")


def test_server_time(api: MEXCSpotAPI) -> bool:
    print("\n[1] Server connectivity")
    try:
        ts = api.get_server_time()
        _ok(f"server time = {ts}")
        return True
    except Exception as exc:
        _fail("get_server_time", str(exc))
        return False


def test_exchange_info_raw(api: MEXCSpotAPI) -> bool:
    """Show the raw field names MEXC returns for BTCUSDT — useful for debugging."""
    print("\n[2a] Exchange info raw fields for BTCUSDT")
    try:
        sample = api.debug_exchange_info_sample("BTCUSDT")
        if not sample:
            _fail("debug_exchange_info_sample", "BTCUSDT not found in response")
            return False
        for k, v in sample.items():
            if k != "filters":
                print(f"        {k}: {v!r}")
        status_val = sample.get("status", "<missing>")
        _ok(f"status field value = {status_val!r}  (must be 'ENABLED' for filter to work)")
        return True
    except Exception as exc:
        _fail("debug_exchange_info_sample", str(exc))
        return False


def test_usdt_spot_symbols(api: MEXCSpotAPI) -> bool:
    print("\n[2b] Exchange info — active USDT spot pairs (status=ENABLED, no isSpotTradingAllowed)")
    try:
        symbols = api.get_all_usdt_spot_symbols()
        count = len(symbols)
        if count == 0:
            _fail("get_all_usdt_spot_symbols",
                  "returned 0 pairs — run test 2a above to see actual status value")
            return False
        sample = sorted(symbols)[:8]
        _ok(f"{count} pairs found.  Sample: {sample}")
        for expected in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            if expected in symbols:
                _ok(f"  {expected} present")
            else:
                _fail(f"  {expected} missing", "check test 2a for actual status field value")
        return True
    except Exception as exc:
        _fail("get_all_usdt_spot_symbols", str(exc))
        return False


def test_klines_columns(api: MEXCSpotAPI) -> bool:
    print("\n[3] Klines — 8-column format + candles_to_df parsing")
    try:
        raw = api.get_klines("BTCUSDT", "60m", limit=5)
        if not raw:
            _fail("get_klines 60m", "empty response")
            return False

        # Check raw column count
        row_len = len(raw[0])
        if row_len != _MEXC_KLINE_NCOLS:
            _fail("column count",
                  f"expected {_MEXC_KLINE_NCOLS} columns, MEXC returned {row_len} — "
                  f"update _MEXC_KLINE_COLS in strategy.py")
            return False
        _ok(f"raw row width = {row_len}  (matches _MEXC_KLINE_NCOLS={_MEXC_KLINE_NCOLS})")
        _ok(f"column names : {_MEXC_KLINE_COLS}")

        # Check candles_to_df
        df = candles_to_df(raw)
        if df.empty:
            _fail("candles_to_df", "returned empty DataFrame")
            return False
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                _fail("candles_to_df", f"missing column '{col}'")
                return False
            if df[col].isna().any():
                _fail("candles_to_df", f"NaN values in '{col}' — type-cast failed")
                return False
        _ok(f"candles_to_df: {len(df)} rows, columns={list(df.columns)}")
        _ok(f"last candle  : open={df['open'].iloc[-1]:.2f}  close={df['close'].iloc[-1]:.2f}")
        return True
    except MEXCAPIError as exc:
        if exc.code == -1121:
            _fail("get_klines 60m", "error -1121: interval still wrong in config.py")
        else:
            _fail("get_klines 60m", str(exc))
        return False
    except Exception as exc:
        _fail("test_klines_columns", str(exc))
        return False


def test_ticker_price(api: MEXCSpotAPI) -> bool:
    print("\n[4] Ticker price (public, no auth header)")
    try:
        price = api.get_ticker_price("BTCUSDT")
        _ok(f"BTCUSDT last price = {price:,.2f} USDT")
        return True
    except Exception as exc:
        _fail("get_ticker_price", str(exc))
        return False


def test_24h_tickers(api: MEXCSpotAPI) -> bool:
    print("\n[5] 24h tickers — spot volume data for CoinSelector")
    try:
        tickers = api.get_24h_tickers()
        usdt_tickers = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
        if not usdt_tickers:
            _fail("get_24h_tickers", "no USDT tickers in response")
            return False
        btc = next((t for t in usdt_tickers if t["symbol"] == "BTCUSDT"), None)
        if btc:
            _ok(f"{len(usdt_tickers)} USDT tickers.  "
                f"BTCUSDT quoteVolume = {float(btc.get('quoteVolume', 0)):,.0f} USDT")
        else:
            _ok(f"{len(usdt_tickers)} USDT tickers returned")
        return True
    except Exception as exc:
        _fail("get_24h_tickers", str(exc))
        return False


def main() -> None:
    api = MEXCSpotAPI()
    results = [
        test_server_time(api),
        test_exchange_info_raw(api),
        test_usdt_spot_symbols(api),
        test_klines_columns(api),
        test_ticker_price(api),
        test_24h_tickers(api),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*40}")
    print(f"  {passed}/{total} tests passed")
    if passed < total:
        print("  Fix the failures above before running the bot.")
        sys.exit(1)
    else:
        print("  All checks passed — bot is ready to run.")


if __name__ == "__main__":
    main()
