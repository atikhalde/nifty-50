"""Synthetic OHLC data + streaming MockFeed for offline testing.

`make_mock_bars()`  -> deterministic dataframe with clear swing highs/lows,
                       equal-high merges and sweeps, so every alert type can
                       be previewed without a network connection.
`MockFeed`          -> feed-compatible object that "streams" those bars:
                       each get_bars() call reveals one more closed bar,
                       so `run_scanner.py --mock` emits alerts progressively
                       exactly like a live session.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from scanner.data.yfinance_feed import INTERVAL_DELTA

TZ = "Asia/Kolkata"


def make_mock_bars(n: int = 420, tf: str = "5m", end: datetime | None = None) -> pd.DataFrame:
    """Deterministic synthetic series with engineered liquidity events."""
    rng = np.random.default_rng(42)
    delta = INTERVAL_DELTA.get(tf, pd.Timedelta(minutes=5))

    if end is None:
        end_ts = pd.Timestamp.now(tz=TZ).floor(delta)
    else:
        end_ts = pd.Timestamp(end)
        end_ts = end_ts.tz_localize(TZ) if end_ts.tz is None else end_ts.tz_convert(TZ)
    idx = pd.date_range(end=end_ts, periods=n, freq=delta, tz=TZ)

    base = 24_300.0
    drift = np.cumsum(rng.normal(0.0, 6.0, n))
    close = base + drift
    spread = np.abs(rng.normal(8.0, 3.0, n)) + 2.0
    high = close + spread * 0.6
    low = close - spread * 0.6
    open_ = close + rng.normal(0.0, 2.0, n)

    def spike_high(i: int, px: float, close_at: float | None = None):
        high[i] = px
        if close_at is not None:
            close[i] = close_at

    def spike_low(i: int, px: float, close_at: float | None = None):
        low[i] = px
        if close_at is not None:
            close[i] = close_at

    # engineered events (well inside the series, away from the warm-up edge)
    for k in range(0, n - 80, 90):
        o = 60 + k
        spike_high(o, close[o] + 90.0)                     # fresh BSL -> SELL
        spike_low(o + 25, close[o + 25] - 90.0)            # fresh SSL -> BUY
        # sweep the BSL: trade through, close back below
        spike_high(o + 45, high[o] + 25.0, close_at=high[o] - 40.0)
        # sweep the SSL: trade through, close back above
        spike_low(o + 55, low[o + 25] - 25.0, close_at=low[o + 25] + 40.0)

    high = np.maximum.reduce([high, open_, close])
    low = np.minimum.reduce([low, open_, close])

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "volume": rng.integers(10_000, 90_000, n).astype(float)},
        index=idx,
    )


class MockFeed:
    """Streams the synthetic series one bar at a time (per timeframe)."""

    def __init__(self, total_bars: int = 420, start_visible: int = 300):
        self.total_bars = total_bars
        self.start_visible = start_visible
        self._full: dict[str, pd.DataFrame] = {}
        self._visible: dict[str, int] = {}

    def get_bars(self, tf: str) -> pd.DataFrame:
        if tf not in self._full:
            # Anchor the LAST revealed bar in the past so every revealed bar
            # is already closed (ts + delta <= now) for the live loop.
            delta = INTERVAL_DELTA.get(tf, pd.Timedelta(minutes=5))
            end = pd.Timestamp.now(tz=TZ).floor(delta) - delta * (self.total_bars - self.start_visible + 2)
            self._full[tf] = make_mock_bars(self.total_bars, tf=tf, end=end)
            self._visible[tf] = self.start_visible

        k = self._visible[tf]
        if k < self.total_bars:
            self._visible[tf] = k + 1
        return self._full[tf].iloc[: self._visible[tf]].copy()
