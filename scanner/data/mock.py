"""Synthetic data for offline previews (--mock / --dump-sample).

`make_mock_bars` builds a deterministic NIFTY-like random-walk series with
enough swings, equal highs/lows and sweeps to exercise every alert type of
the BSL/SSL engine (BUY, SELL, sweep, merge). No network needed.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from scanner.data.yfinance_feed import INTERVAL_DELTA

_TF_MIN = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


def make_mock_bars(n: int = 600, tf_minutes: int = 5, seed: int = 42,
                   start_price: float = 24300.0, tz: str = "Asia/Kolkata",
                   start: str = "2026-08-24 09:15") -> pd.DataFrame:
    """Deterministic NIFTY-like OHLC series (0.05 tick, session-time index)."""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=float)

    # random walk + slow sine: the sine guarantees frequent, clearly separated
    # swings (pivot confirmations) while the noise creates flat/twin extremes
    drift = np.cumsum(rng.normal(0.0, 1.6, n))
    wave = 45.0 * np.sin(2 * np.pi * t / 97.0)
    close = start_price + drift + wave
    close = np.round(close / 0.05) * 0.05

    open_ = np.empty(n)
    open_[0] = start_price
    open_[1:] = close[:-1]
    spread_hi = np.abs(rng.normal(0.0, 1.3, n))
    spread_lo = np.abs(rng.normal(0.0, 1.3, n))
    high = np.round((np.maximum(open_, close) + spread_hi) / 0.05) * 0.05
    low = np.round((np.minimum(open_, close) - spread_lo) / 0.05) * 0.05

    idx = pd.date_range(start, periods=n, freq=f"{tf_minutes}min", tz=tz)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


class MockFeed:
    """Simulates a live feed: bars 'close' one per `bar_secs` wall-seconds.

    The deterministic series starts with WARMUP bars of history whose tail is
    anchored at the wall clock (the last warmup bar is 'forming' at start-up),
    followed by pre-generated 'future' bars revealed one at a time. Timestamps
    are stable (a bar's stamp never changes between calls, so alert dedupe
    behaves exactly like the live path), but stamps advance `bar_secs` apart —
    i.e. the demo clock runs faster than the nominal timeframe.

    So `python run_scanner.py --mock` streams alerts exactly like the live
    scanner, without network or Telegram.
    """

    WARMUP = 600

    def __init__(self, seed: int = 42, bar_secs: float = 1.0,
                 tz: str = "Asia/Kolkata"):
        self.seed = seed
        self.bar_secs = bar_secs
        self.tz = tz
        self._t0 = time.time()
        self._frames: dict[str, pd.DataFrame] = {}

    def get_bars(self, tf: str) -> pd.DataFrame:
        mins = _TF_MIN.get(tf, 5)
        if tf not in self._frames:
            df = make_mock_bars(n=self.WARMUP + 400, tf_minutes=mins,
                                seed=self.seed + mins, tz=self.tz)
            n = len(df)
            step = pd.to_timedelta(self.bar_secs, unit="s")
            delta = INTERVAL_DELTA.get(tf, pd.Timedelta(minutes=5))
            now0 = pd.Timestamp.now(tz=self.tz)
            # bar WARMUP-1 is 'forming' at start-up (its close time is one step
            # in the future); bar WARMUP-2 is the last already-closed bar.
            anchor = now0 - delta + step
            df.index = pd.DatetimeIndex(
                anchor + pd.to_timedelta((np.arange(n) - (self.WARMUP - 1)) * self.bar_secs, unit="s")
            )
            self._frames[tf] = df

        df = self._frames[tf]
        reveal = int((time.time() - self._t0) // self.bar_secs)
        return df.iloc[: min(len(df), self.WARMUP + reveal)]
