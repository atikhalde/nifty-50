"""Yahoo Finance bar feed for the scanner.

Provides a warm-up window (enough history for the pool state to converge with
a TradingView chart) plus an incremental cache so only new bars are fetched on
each scan cycle. Intraday bars are filtered to the NSE session
(09:15-15:30 IST) so they match TradingView.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

# Interval -> (pandas freq, lookback depth, warm-up period, yfinance period)
INTERVAL_DELTA = {
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
}
_PRELOAD = {
    # yfinance only serves ~7 days of 1m bars (and 5m bars with a 60d period)
    "1m": ("7d", "7d"),
    "5m": ("60d", "60d"),
}


class YFinanceFeed:
    def __init__(self, symbol: str = "^NSEI", tz: str = "Asia/Kolkata",
                 session_start: str = "09:15", session_end: str = "15:30"):
        self.symbol = symbol
        self.tz = ZoneInfo(tz)
        self.session_start = session_start
        self.session_end = session_end
        self._cache: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    def get_bars(self, tf: str) -> pd.DataFrame | None:
        """Return cached+refreshed OHLC bars for `tf` (index tz-aware, tz=self.tz).

        Always returns the FULL warm-up window — the engine needs the history
        to converge its pool state (pivot confirmation needs 8 bars each side).
        Lookback mode slices the *alerting* window in live.py, not here.
        """
        period, preload = _PRELOAD.get(tf, ("60d", "60d"))
        df = self._refresh(tf, period, preload)
        if df is None or df.empty:
            return None
        return self._session_filter(df)

    # ------------------------------------------------------------------
    def _refresh(self, tf: str, period: str, preload: str) -> pd.DataFrame | None:
        cached = self._cache.get(tf)
        if cached is None:
            df = self._download(tf, period=preload)      # warm-up window
        else:
            last = cached.index.max()
            if datetime.now(self.tz) - last > pd.Timedelta(minutes=7):
                # re-download the whole window — yfinance may have revised bars
                df = self._download(tf, period=period)
            else:
                start = (last - pd.Timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
                df = self._download(tf, period=period, start=start)
                if df is not None and not df.empty:
                    df = pd.concat([cached, df])
        if df is None or df.empty:
            return cached
        df = self._normalize(df)
        self._cache[tf] = df
        return df

    def _download(self, tf: str, period: str = "7d",
                  start: str | None = None) -> pd.DataFrame | None:
        import yfinance as yf
        try:
            if start:
                start = (pd.Timestamp(start) + pd.Timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
                df = yf.download(
                    self.symbol, interval=tf, period=period,
                    start=start, progress=False, auto_adjust=False,
                )
            else:
                df = yf.download(
                    self.symbol, interval=tf, period=period,
                    progress=False, auto_adjust=False,
                )
            return df if df is not None and not df.empty else None
        except Exception:
            log.exception("yfinance fetch failed (tf=%s)", tf)
            return None

    @staticmethod
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Kolkata")
        else:
            df.index = df.index.tz_convert("Asia/Kolkata")
        return df

    def _session_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """Keep only bars whose timestamp falls inside the NSE session."""
        s, e = self.session_start, self.session_end
        mask = df.index.to_series().apply(
            lambda ts: s <= ts.strftime("%H:%M") <= e)
        return df[mask]
