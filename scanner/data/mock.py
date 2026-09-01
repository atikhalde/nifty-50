"""Synthetic data feed for offline preview / demo (no network, no Telegram).

``make_mock_bars()`` builds a deterministic OHLC frame (tz-aware IST index,
lowercase columns) engineered to contain clear swing highs/lows — so the
BSL/SSL engine produces BUY, SELL and sweep signals you can preview with::

    python run_scanner.py --dump-sample
    python run_scanner.py --mock

``MockFeed`` replays that frame incrementally so ``run_scanner.py --mock``
behaves like a live stream: each ``get_bars`` call reveals one more closed bar.
"""Synthetic OHLC feed for offline previews and self-tests.

Generates deterministic NIFTY-like 1m/5m bars (seeded RNG) with a gentle
trend + volatility waves, plus a few sharp swing spikes so the BSL/SSL engine
produces real pools, sweeps and signals without any network access.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_TZ = "Asia/Kolkata"

# columns produced, matching the yfinance feed contract
_COLS = ["open", "high", "low", "close", "volume"]


def make_mock_bars(n: int = 260, base: float = 24_000.0, seed: int = 7) -> pd.DataFrame:
    """Deterministic synthetic NIFTY-like 5m frame with engineered swings.

    Bars are placed on a continuous 5-minute grid inside the NSE session
    (09:15–15:30 IST) across consecutive weekdays, so they survive the feed's
    session filter and line up like real intraday bars.
    """
    rng = np.random.default_rng(seed)

    # --- a gentle random walk as the baseline close ---
    steps = rng.normal(0.0, 8.0, size=n)
    close = base + np.cumsum(steps)

    open_ = np.empty(n)
    high = np.empty(n)
    low = np.empty(n)
    open_[0] = close[0]
    for i in range(1, n):
        open_[i] = close[i - 1]

    # base high/low around the open/close with a little noise
    for i in range(n):
        hi = max(open_[i], close[i]) + abs(rng.normal(0, 4.0)) + 3.0
        lo = min(open_[i], close[i]) - abs(rng.normal(0, 4.0)) - 3.0
        high[i] = hi
        low[i] = lo

    # --- engineer distinct swing highs (→ BSL pools → SELL signals) ---
    for i in (30, 70, 150):
        if i < n:
            high[i] = max(high[i], close[i - 1] + 90.0)
            # keep it a strict local extreme
            for k in range(1, 9):
                if i - k >= 0:
                    high[i - k] = min(high[i - k], high[i] - 20.0)
                if i + k < n:
                    high[i + k] = min(high[i + k], high[i] - 20.0)

    # --- engineer distinct swing lows (→ SSL pools → BUY signals) ---
    for i in (50, 110, 190):
        if i < n:
            low[i] = min(low[i], close[i - 1] - 90.0)
            for k in range(1, 9):
                if i - k >= 0:
                    low[i - k] = max(low[i - k], low[i] + 20.0)
                if i + k < n:
                    low[i + k] = max(low[i + k], low[i] + 20.0)

    # --- engineer a couple of sweeps (trade through a prior level, close back) ---
    # sweep the BSL pool created from the swing high at bar 30 (~confirmed @38)
    if 95 < n:
        lvl = high[30]
        high[95] = lvl + 25.0
        close[95] = lvl - 15.0
        open_[95] = lvl - 5.0
        low[95] = min(low[95], close[95] - 10.0)
    # sweep the SSL pool created from the swing low at bar 50 (~confirmed @58)
    if 130 < n:
        lvl = low[50]
        low[130] = lvl - 25.0
        close[130] = lvl + 15.0
        open_[130] = lvl + 5.0
        high[130] = max(high[130], close[130] + 10.0)

    # --- fix any invariant violations (high must be the max, low the min) ---
    for i in range(n):
        high[i] = max(high[i], open_[i], close[i], low[i])
        low[i] = min(low[i], open_[i], close[i], high[i])

    idx = _session_index(n)
    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(1_000, 50_000, size=n).astype(float),
        },
        index=idx,
    )
    return df


def _session_index(n: int) -> pd.DatetimeIndex:
    """Build ``n`` consecutive 5-minute timestamps inside the NSE session,
    rolling over to the next weekday when a day's session is exhausted."""
    # 09:15 -> 15:30 inclusive at 5m = 76 bars per day
    per_day = 76
    stamps: list[pd.Timestamp] = []
    day = pd.Timestamp("2026-08-03 09:15", tz=_TZ)  # a Monday
    while len(stamps) < n:
        if day.weekday() < 5:
            start = day.normalize() + pd.Timedelta(hours=9, minutes=15)
            for k in range(per_day):
                if len(stamps) >= n:
                    break
                stamps.append(start + k * pd.Timedelta(minutes=5))
        day = day + pd.Timedelta(days=1)
    return pd.DatetimeIndex(stamps)


class MockFeed:
    """Replays a synthetic frame incrementally to emulate a live stream."""

    def __init__(self, df: pd.DataFrame | None = None, start_at: int = 60):
        self._df = df if df is not None else make_mock_bars()
        # reveal a warm-up chunk first, then one more bar per get_bars call
        self._cursor = min(max(start_at, 2), len(self._df))

    def get_bars(self, tf: str) -> pd.DataFrame:
        # timeframe is ignored for the mock — same synthetic series for all tf
        window = self._df.iloc[: self._cursor].copy()
        if self._cursor < len(self._df):
            self._cursor += 1
        return window
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
