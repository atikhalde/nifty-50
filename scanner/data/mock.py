"""Synthetic OHLC feed for offline previews and self-tests.

Generates deterministic NIFTY-like 1m/5m bars (seeded RNG) with a gentle
trend + volatility waves, plus a few sharp swing spikes so the BSL/SSL engine
produces real pools, sweeps and signals without any network access.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

# NIFTY-ish starting level so messages look realistic
BASE = 24300.0
RNG_SEED = 7

INTERVAL_DELTA = {
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
}


def make_mock_bars(n: int = 300, tf: str = "5m") -> pd.DataFrame:
    """Deterministic synthetic OHLC bars, tz-aware Asia/Kolkata index.

    Sine + drift price path with a few engineered swing spikes (high above the
    path, low below it) so pivot-based pools reliably form.
    """
    rng = np.random.default_rng(RNG_SEED)
    freq = {"1m": "1min", "5m": "5min"}.get(tf, "5min")  # pandas>=2.2 alias
    idx = pd.date_range("2026-08-31 09:15", periods=n, freq=freq,
                        tz="Asia/Kolkata")
    t = np.arange(n)
    path = BASE + 60 * np.sin(t / 25.0) + t * 0.5 + rng.normal(0, 12, n).cumsum() * 0.3
    amp = 25 + 8 * np.abs(np.sin(t / 40.0))

    open_ = np.roll(path, 1)
    open_[0] = path[0]
    close = path
    high = np.maximum(open_, close) + amp
    low = np.minimum(open_, close) - amp

    # engineered swing spikes -> guaranteed pools/sweeps for previews
    high[40] += 130.0
    low[70] -= 130.0
    high[120] += 90.0
    low[160] -= 90.0

    df = pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.integers(50_000, 200_000, n),
    }, index=idx)
    return df.round(2)


class MockFeed:
    """Drop-in replacement for YFinanceFeed (same get_bars contract)."""

    def __init__(self, symbol: str = "^NSEI", tz: str = "Asia/Kolkata",
                 session_start: str = "09:15", session_end: str = "15:30"):
        self.symbol = symbol
        self.tz = tz
        self._cache: dict[str, pd.DataFrame] = {}

    def get_bars(self, tf: str) -> pd.DataFrame | None:
        df = self._cache.get(tf)
        if df is None:
            df = make_mock_bars(tf=tf)
            self._cache[tf] = df
        return df
