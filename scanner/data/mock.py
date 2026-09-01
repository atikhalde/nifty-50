"""Deterministic synthetic feed — lets you exercise the full alert path offline.

`make_mock_bars()` builds a NIFTY-like 5-minute series with engineered swing
highs/lows and sweeps so BUY, SELL and 🧹 sweep alerts are all guaranteed to
appear. `MockFeed` replays it bar by bar so `--mock` behaves like a live feed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scanner.data.yfinance_feed import INTERVAL_DELTA  # noqa: F401  (re-export parity)

TZ = "Asia/Kolkata"


def make_mock_bars(n: int = 400, seed: int = 7, start_price: float = 24_300.0):
    """Random-walk OHLC with injected swings and sweeps (deterministic)."""
    rng = np.random.default_rng(seed)

    # Session-shaped index: 5-minute bars starting at an NSE open.
    idx = pd.date_range("2026-08-03 09:15", periods=n, freq="5min", tz=TZ)

    steps = rng.normal(0.0, 12.0, n).cumsum()
    close = start_price + steps
    open_ = np.empty(n)
    open_[0] = start_price
    open_[1:] = close[:-1]

    spread = np.abs(rng.normal(0.0, 9.0, n)) + 3.0
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread

    # --- engineered swing highs (open BSL pools -> SELL under fade mapping) ---
    for i in (40, 110, 190, 275, 340):
        if i < n:
            high[i] = max(high[i], np.max(high[max(0, i - 12):i + 13]) + 60.0)

    # --- engineered swing lows (open SSL pools -> BUY) ---
    for i in (70, 145, 225, 305):
        if i < n:
            low[i] = min(low[i], np.min(low[max(0, i - 12):i + 13]) - 60.0)

    # --- engineered sweeps: pierce a prior swing level, close back inside ---
    for src, hit in ((40, 62), (110, 132), (70, 96), (145, 170)):
        if hit >= n or src >= n:
            continue
        if src in (40, 110):                      # BSL sweep: high > lvl, close < lvl
            lvl = high[src]
            high[hit] = lvl + 25.0
            close[hit] = lvl - 30.0
            open_[hit] = lvl - 10.0
            low[hit] = min(low[hit], close[hit] - 15.0)
        else:                                      # SSL sweep: low < lvl, close > lvl
            lvl = low[src]
            low[hit] = lvl - 25.0
            close[hit] = lvl + 30.0
            open_[hit] = lvl + 10.0
            high[hit] = max(high[hit], close[hit] + 15.0)

    # Enforce OHLC integrity after the injections.
    high = np.maximum.reduce([high, open_, close])
    low = np.minimum.reduce([low, open_, close])

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(50_000, 250_000, n).astype(float),
        },
        index=idx,
    )


class MockFeed:
    """Replays synthetic bars, revealing one more bar on each `get_bars` call."""

    def __init__(self, bars=None, warmup: int = 120, step: int = 1):
        self._bars = bars if bars is not None else make_mock_bars()
        self._warmup = max(2, int(warmup))
        self._step = max(1, int(step))
        self._cursor: dict[str, int] = {}

    def get_bars(self, interval: str, lookback_period: str | None = None):
        n = len(self._bars)
        cur = self._cursor.get(interval, self._warmup)
        cur = min(cur, n)
        df = self._bars.iloc[:cur].copy()
        self._cursor[interval] = min(cur + self._step, n)

        # Re-stamp so the newest bars look "just closed" relative to now, which
        # is what the live scanner's closed-bar filter expects.
        delta = INTERVAL_DELTA.get(interval, pd.Timedelta(minutes=5))
        end = pd.Timestamp.now(tz=TZ).floor(delta)
        df.index = pd.date_range(end=end, periods=len(df), freq=delta, tz=TZ)
        return df
