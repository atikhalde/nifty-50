"""yfinance OHLC feed for the NIFTY index (^NSEI).

Design goals (see README):
  * Warm up with enough history that the stateful BSL/SSL engine converges to
    the TradingView chart: ~7 days for 1m (Yahoo's hard limit for 1m data),
    ~60 days for other intraday intervals (Yahoo's limit for sub-daily data).
    Pool state has bounded memory (POOL_EXPIRY bars, default 300) and the
    Wilder RMA behind ATR(14) converges exponentially, so after ~300 bars of
    identical data the engine state matches the chart exactly.
  * Filter strictly to the NSE regular session 09:15-15:30 IST. Yahoo
    occasionally includes pre-open prints / stray rows for indices; dropping
    them is required for pivot windows to align with TradingView.
  * Bars are timestamped at bar START (same convention as TradingView), so a
    5m bar stamped 09:15 covers 09:15:00-09:20:00.
  * Light caching so a 20s scan loop does not hammer Yahoo (which throttles);
    refetch only when a new bar could have closed or after min_refetch_sec.
"""

from __future__ import annotations

import logging
import time
from datetime import time as dtime

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# scanner timeframe -> bar length (bar at index ts is CLOSED at ts + delta)
INTERVAL_DELTA: dict[str, pd.Timedelta] = {
    "1m": pd.Timedelta(minutes=1),
    "2m": pd.Timedelta(minutes=2),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(minutes=60),
    "1d": pd.Timedelta(days=1),
}

# yfinance interval strings for the timeframes we support
_YF_INTERVAL = {
    "1m": "1m",
    "2m": "2m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "60m",
    "1d": "1d",
}

# warm-up depth: Yahoo allows max 7d of 1m and 60d of other intraday data
_WARMUP_PERIOD = {"1m": "7d", "1d": "1y"}
_DEFAULT_WARMUP = "60d"


class YFinanceFeed:
    """Incremental OHLC feed: Yahoo -> tz-aware, session-filtered DataFrame."""

    def __init__(self, symbol: str = "^NSEI", tz: str = "Asia/Kolkata",
                 session_start: str = "09:15", session_end: str = "15:30",
                 min_refetch_sec: float = 8.0):
        self.symbol = symbol
        self.tz = tz
        self.session_start = dtime.fromisoformat(session_start)
        self.session_end = dtime.fromisoformat(session_end)
        self.min_refetch_sec = min_refetch_sec
        self._ticker = yf.Ticker(symbol)
        self._cache: dict[str, tuple[float, pd.DataFrame]] = {}

    # ------------------------------------------------------------------
    def get_bars(self, tf: str) -> pd.DataFrame:
        """Return warm-up + latest bars for `tf` (columns: open/high/low/close,
        tz-aware start-time index, session-filtered 09:15-15:30)."""
        if tf not in _YF_INTERVAL:
            raise ValueError(f"unsupported timeframe {tf!r} (supported: {sorted(_YF_INTERVAL)})")

        cached = self._cache.get(tf)
        if cached is not None:
            fetched_at, cdf = cached
            delta = INTERVAL_DELTA.get(tf, pd.Timedelta(minutes=5))
            now = pd.Timestamp.now(tz=self.tz)
            forming_open = len(cdf) and (cdf.index[-1] + delta) > now
            if forming_open and (time.monotonic() - fetched_at) < self.min_refetch_sec:
                return cdf  # newest cached bar is still forming; nothing new yet

        period = _WARMUP_PERIOD.get(tf, _DEFAULT_WARMUP)
        try:
            df = self._ticker.history(period=period, interval=_YF_INTERVAL[tf],
                                      auto_adjust=False, prepost=False)
            df = self._normalize(df)
        except Exception:
            log.exception("yfinance fetch failed for %s (%s)", self.symbol, tf)
            if cached is not None:
                log.warning("returning stale cached bars for %s", tf)
                return cached[1]
            raise

        if df.empty:
            log.warning("yfinance returned no usable bars for %s (%s)", self.symbol, tf)
            if cached is not None:
                return cached[1]
        else:
            self._cache[tf] = (time.monotonic(), df)
        return df

    # ------------------------------------------------------------------
    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close"])

        df = df.rename(columns={c: str(c).lower() for c in df.columns})
        if isinstance(df.columns, pd.MultiIndex):  # defensive: newer yf layouts
            df.columns = df.columns.get_level_values(0)
            df = df.rename(columns={c: str(c).lower() for c in df.columns})

        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        df.index = idx.tz_convert(self.tz)

        # NSE regular session only — drops pre-open/auction prints that Yahoo
        # sometimes includes for indices (they would shift pivot windows vs TV)
        t = df.index.time
        df = df[(t >= self.session_start) & (t < self.session_end)]

        df = df[["open", "high", "low", "close"]].dropna()
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
