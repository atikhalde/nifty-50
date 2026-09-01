"""yfinance data feed for the BSL/SSL scanner.

Responsibilities:
  * warm-up fetch (enough history for the pool engine to converge:
    1m -> ~7 days, 5m/15m -> ~60 days), then cheap incremental fetches;
  * normalise everything the engine depends on:
      - lower-case OHLC columns,
      - tz-aware index converted to the exchange timezone (IST),
      - NaN rows dropped,
      - duplicate timestamps de-duplicated (keep the latest snapshot),
      - bars filtered to the NSE session (09:15 <= t < 15:30, like TradingView);
  * be resilient in live markets: if Yahoo throttles or the network blips,
    the last good cache is returned instead of raising, so one bad fetch
    never kills a scan cycle.
"""

from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

# Bar duration per timeframe — used by the live loop to decide which bars are CLOSED.
INTERVAL_DELTA: dict[str, pd.Timedelta] = {
    "1m": pd.Timedelta(minutes=1),
    "2m": pd.Timedelta(minutes=2),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "60m": pd.Timedelta(minutes=60),
    "1h": pd.Timedelta(hours=1),
}

# Yahoo's own per-interval history limits (1m: 7 days max per request).
WARMUP_PERIOD: dict[str, str] = {
    "1m": "7d",
    "2m": "60d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "180d",
    "1h": "180d",
}

# Incremental refresh window: generous enough to bridge weekends/holidays
# so bars are never missed between two ticks, small enough to be cheap.
REFRESH_PERIOD: dict[str, str] = {
    "1m": "1d",
    "2m": "5d",
    "5m": "5d",
    "15m": "5d",
    "30m": "5d",
    "60m": "5d",
    "1h": "5d",
}

# Keep plenty of history for the engine (pool_expiry=300 bars + pivots) while
# bounding memory/CPU. 3000 bars ≈ 8 sessions of 1m / 40 sessions of 5m.
MAX_CACHE_BARS = 3000


class YFinanceFeed:
    def __init__(self, symbol: str = "^NSEI", tz: str = "Asia/Kolkata",
                 session_start: str = "09:15", session_end: str = "15:30",
                 max_cache_bars: int = MAX_CACHE_BARS):
        self.symbol = symbol
        self.tz = tz
        self.session_start = session_start
        self.session_end = session_end
        self.max_cache_bars = max_cache_bars
        self._cache: dict[str, pd.DataFrame] = {}
        self._ticker = None  # lazy — never touch the network at construction

    # ------------------------------------------------------------------
    def get_bars(self, tf: str) -> pd.DataFrame | None:
        """Return the OHLC dataframe for `tf` (index = bar START time, IST).

        Includes the currently-forming bar; the live loop is responsible for
        only acting on CLOSED bars (ts + INTERVAL_DELTA <= now).
        Never raises on fetch problems: logs and returns the cached data
        (or None when there is no cache yet).
        """
        if tf not in INTERVAL_DELTA:
            log.error("unsupported timeframe %r (supported: %s)", tf, ",".join(INTERVAL_DELTA))
            return None

        period = WARMUP_PERIOD[tf] if tf not in self._cache else REFRESH_PERIOD[tf]
        fresh = self._fetch(tf, period)

        if fresh is None or fresh.empty:
            cached = self._cache.get(tf)
            if cached is not None:
                log.warning("[%s] fetch returned nothing — using cached %d bars", tf, len(cached))
                return cached.copy()
            return None

        cached = self._cache.get(tf)
        if cached is not None and not cached.empty:
            # New snapshot wins on overlapping timestamps (it has the latest
            # values for the previously-forming bar).
            merged = pd.concat([cached, fresh])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        else:
            merged = fresh
            log.info("[%s] warm-up complete: %d bars (%s → %s)", tf, len(merged),
                     merged.index[0], merged.index[-1])

        if len(merged) > self.max_cache_bars:
            merged = merged.iloc[-self.max_cache_bars:]

        self._cache[tf] = merged
        return merged.copy()

    # ------------------------------------------------------------------
    def _fetch(self, tf: str, period: str) -> pd.DataFrame | None:
        try:
            if self._ticker is None:
                import yfinance as yf
                self._ticker = yf.Ticker(self.symbol)
            raw = self._ticker.history(
                period=period, interval=tf,
                auto_adjust=False, prepost=False,
                actions=False, raise_errors=False,
            )
        except Exception:
            log.exception("[%s] yfinance fetch failed", tf)
            return None
        try:
            return self._normalize(raw)
        except Exception:
            log.exception("[%s] could not normalise yfinance data", tf)
            return None

    # ------------------------------------------------------------------
    def _normalize(self, raw: pd.DataFrame | None) -> pd.DataFrame | None:
        if raw is None or len(raw) == 0:
            return None
        df = raw.copy()

        # yf.download() can return MultiIndex columns; Ticker.history() is flat.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c).strip().lower() for c in df.columns]

        needed = ["open", "high", "low", "close"]
        if any(c not in df.columns for c in needed):
            log.error("missing OHLC columns in yfinance response: %s", list(df.columns))
            return None
        df = df[needed + (["volume"] if "volume" in df.columns else [])]

        # tz-aware index in the exchange timezone
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        df.index = idx.tz_convert(self.tz)

        # drop rows without prices, dedupe, sort
        df = df.dropna(subset=needed)
        df = df[~df.index.duplicated(keep="last")].sort_index()

        # NSE session filter (like TradingView): keep 09:15 <= t < 15:30.
        # Yahoo sometimes emits a stray 15:30 auction bar — strict '<' drops it.
        df = df[(df.index.time >= _t(self.session_start)) & (df.index.time < _t(self.session_end))]
        return df if not df.empty else None


def _t(hhmm: str):
    from datetime import time as dtime
    return dtime.fromisoformat(hhmm)
