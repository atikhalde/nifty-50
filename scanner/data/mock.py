"""Synthetic mock data feed for offline testing and demonstration."""

from __future__ import annotations

from datetime import datetime
import numpy as np
import pandas as pd

INTERVAL_DELTA = {
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
    "1d": pd.Timedelta(days=1),
}


def make_mock_bars(n: int = 240, freq: str = "5min", tz: str = "Asia/Kolkata") -> pd.DataFrame:
    """Generate realistic synthetic NIFTY 5m bars featuring:
      - Confirmed swing lows & highs
      - Swept liquidity pools (BSL and SSL)
      - Non-repainting 8-bar pivot confirmations
      - Realistic NIFTY price levels (~24,000 - 24,200)
    """
    now = pd.Timestamp.now(tz=tz).floor("5min")
    start_time = now - pd.Timedelta(minutes=5 * n)
    idx = pd.date_range(start=start_time, periods=n, freq=freq, tz=tz)

    # Base price path with trend and oscillations
    t = np.linspace(0, 8 * np.pi, n)
    trend = np.linspace(24000, 24150, n)
    wave = 60.0 * np.sin(t) + 30.0 * np.cos(2 * t)
    base = trend + wave

    # Add candles
    np.random.seed(42)
    noise = np.random.normal(0, 3.0, n)
    close = base + noise
    open_p = np.roll(close, 1)
    open_p[0] = close[0] - 2.0

    high = np.maximum(open_p, close) + np.abs(np.random.normal(4.0, 2.0, n))
    low = np.minimum(open_p, close) - np.abs(np.random.normal(4.0, 2.0, n))

    df = pd.DataFrame({
        "open": np.round(open_p, 2),
        "high": np.round(high, 2),
        "low": np.round(low, 2),
        "close": np.round(close, 2),
        "volume": np.random.randint(10000, 50000, size=n),
    }, index=idx)

    # Inject specific swing and sweep patterns to guarantee signals
    # 1. Swing low at bar 30 -> confirmed at bar 38
    df.iloc[30, df.columns.get_loc("low")] = 23980.0
    df.iloc[30, df.columns.get_loc("close")] = 24000.0

    # 2. Swing high at bar 60 -> confirmed at bar 68
    df.iloc[60, df.columns.get_loc("high")] = 24200.0
    df.iloc[60, df.columns.get_loc("close")] = 24160.0

    # 3. Sweep of SSL @ 23980 at bar 100
    df.iloc[100, df.columns.get_loc("low")] = 23970.0
    df.iloc[100, df.columns.get_loc("close")] = 23990.0

    # 4. Sweep of BSL @ 24200 at bar 140
    df.iloc[140, df.columns.get_loc("high")] = 24210.0
    df.iloc[140, df.columns.get_loc("close")] = 24180.0

    # 5. Fresh swing low at bar 180 -> confirmed at bar 188
    df.iloc[180, df.columns.get_loc("low")] = 24010.55
    df.iloc[180, df.columns.get_loc("close")] = 24030.0

    # 6. Fresh swing high at bar 210 -> confirmed at bar 218
    df.iloc[210, df.columns.get_loc("high")] = 24142.85
    df.iloc[210, df.columns.get_loc("close")] = 24125.0

    return df


class MockFeed:
    """Mock streaming feed for simulation and live tests."""

    def __init__(self, n_bars: int = 240, tz: str = "Asia/Kolkata"):
        self.tz = tz
        self.full_df = make_mock_bars(n=n_bars, tz=tz)
        self.current_idx = min(60, len(self.full_df))

    def get_bars(self, tf: str = "5m") -> pd.DataFrame:
        """Return slice of mock bars progressing over time."""
        if self.current_idx < len(self.full_df):
            self.current_idx += 1
        return self.full_df.iloc[:self.current_idx].copy()
