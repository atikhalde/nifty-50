"""YFinance data feed for NSE:NIFTY with caching and session filtering."""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    yf = None

log = logging.getLogger(__name__)

INTERVAL_DELTA = {
    "1m": pd.Timedelta(minutes=1),
    "2m": pd.Timedelta(minutes=2),
    "3m": pd.Timedelta(minutes=3),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
    "1d": pd.Timedelta(days=1),
}

TF_PERIOD_MAP = {
    "1m": "7d",
    "2m": "30d",
    "3m": "30d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "1h": "730d",
    "1d": "max",
}


class YFinanceFeed:
    """Fetches intraday bars for NSE:NIFTY (^NSEI) from Yahoo Finance."""

    def __init__(
        self,
        symbol: str = "^NSEI",
        tz: str = "Asia/Kolkata",
        session_start: str = "09:15",
        session_end: str = "15:30",
    ):
        self.symbol = symbol
        self.tz = ZoneInfo(tz)
        self.session_start = dtime.fromisoformat(session_start)
        self.session_end = dtime.fromisoformat(session_end)
        self._cache: dict[str, pd.DataFrame] = {}

    def get_bars(self, tf: str = "5m") -> pd.DataFrame:
        """Fetch and return cleaned OHLC dataframe for the requested timeframe."""
        period = TF_PERIOD_MAP.get(tf, "60d")
        try:
            df = self._fetch_yf(tf, period)
            if df is not None and not df.empty:
                df = self._clean_and_filter(df)
                self._cache[tf] = df
                return df
        except Exception as e:
            log.warning("yfinance fetch failed for %s (%s): %s", self.symbol, tf, e)

        # Fallback to last known good cache if available
        if tf in self._cache:
            log.info("Using cached data for %s (%s)", self.symbol, tf)
            return self._cache[tf]

        return pd.DataFrame()

    def _fetch_yf(self, interval: str, period: str) -> pd.DataFrame:
        if yf is None:
            raise RuntimeError("yfinance is not installed")
        ticker = yf.Ticker(self.symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=False)
        return df

    def _clean_and_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize column names, ensure timezone, and filter to market hours."""
        df = df.copy()
        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        df.columns = [str(c).lower().strip() for c in df.columns]

        required = ["open", "high", "low", "close"]
        for col in required:
            if col not in df.columns:
                raise ValueError(f"Missing required column '{col}' in data feed")

        # Convert index to target timezone
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(self.tz)
        else:
            df.index = df.index.tz_convert(self.tz)

        # Drop NaN or zero prices
        df = df.dropna(subset=required)
        df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)]

        # Filter by NSE trading hours
        times = df.index.time
        mask = (times >= self.session_start) & (times <= self.session_end)
        df = df.loc[mask]

        # Ensure sorted
        df = df.sort_index()
        # Drop duplicates
        df = df[~df.index.duplicated(keep="last")]

        return df
