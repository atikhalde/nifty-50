"""Yahoo Finance intraday feed for NSE:NIFTY (^NSEI).

Design goals (live-market safety):
  * NEVER raise into the scan loop — every failure returns None and is logged.
  * Normalise whatever yfinance hands back (MultiIndex columns, naive index,
    duplicate/uns0rted timestamps, NaN rows) into a clean OHLCV frame with a
    tz-aware Asia/Kolkata index.
  * Short-TTL cache so a 20 s scan cadence does not hammer Yahoo, and a
    stale-data guard so we never re-run the engine on a frozen feed silently.
  * Bounded retries with backoff on transient network errors.
"""

from __future__ import annotations

import logging
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

# Bar length per supported interval — used by the scanner to decide which bars
# have actually CLOSED (Yahoo timestamps a bar by its OPEN time).
INTERVAL_DELTA: dict[str, pd.Timedelta] = {
    "1m": pd.Timedelta(minutes=1),
    "2m": pd.Timedelta(minutes=2),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "60m": pd.Timedelta(minutes=60),
    "1h": pd.Timedelta(minutes=60),
    "1d": pd.Timedelta(days=1),
}

# Yahoo's own history limits for intraday intervals. Asking for more than this
# returns an empty frame, which used to look like "market closed".
INTERVAL_PERIOD: dict[str, str] = {
    "1m": "5d",
    "2m": "5d",
    "5m": "1mo",
    "15m": "1mo",
    "30m": "1mo",
    "60m": "3mo",
    "1h": "3mo",
    "1d": "1y",
}

# Minimum bars the engine needs (pivLen*2+1 plus ATR warm-up) to be meaningful.
MIN_BARS = 60


class FeedError(RuntimeError):
    pass


class YFinanceFeed:
    def __init__(self, symbol: str = "^NSEI", tz: str = "Asia/Kolkata",
                 session_start: str = "09:15", session_end: str = "15:30",
                 cache_ttl_sec: int = 15, max_retries: int = 3):
        self.symbol = symbol
        self.tzname = tz
        self.tz = ZoneInfo(tz)
        self.session_start = session_start
        self.session_end = session_end
        self.cache_ttl_sec = max(0, int(cache_ttl_sec))
        self.max_retries = max(1, int(max_retries))

        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, pd.DataFrame]] = {}
        self._last_bar_ts: dict[str, pd.Timestamp] = {}
        self._stale_since: dict[str, float] = {}

    # ------------------------------------------------------------------
    def get_bars(self, interval: str, lookback_period: str | None = None):
        """Return a clean OHLCV DataFrame, or None if unavailable.

        Columns: open, high, low, close, volume. Index: tz-aware, sorted,
        unique. Never raises.
        """
        if interval not in INTERVAL_DELTA:
            log.error("Unsupported interval %r — supported: %s",
                      interval, ", ".join(sorted(INTERVAL_DELTA)))
            return None

        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(interval)
            if hit and (now - hit[0]) < self.cache_ttl_sec:
                return hit[1]

        period = lookback_period or INTERVAL_PERIOD.get(interval, "5d")
        df = self._download(interval, period)
        if df is None:
            # Serve the last good frame rather than blinding the scanner.
            with self._lock:
                hit = self._cache.get(interval)
            if hit is not None:
                log.warning("[%s] using cached bars (age %.0fs) after download failure",
                            interval, now - hit[0])
                return hit[1]
            return None

        self._check_staleness(interval, df)
        with self._lock:
            self._cache[interval] = (now, df)
        return df

    # ------------------------------------------------------------------
    def _download(self, interval: str, period: str):
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                import yfinance as yf

                raw = yf.download(
                    tickers=self.symbol,
                    period=period,
                    interval=interval,
                    auto_adjust=False,
                    prepost=False,
                    progress=False,
                    threads=False,
                )
                df = self._normalise(raw)
                if df is None or df.empty:
                    raise FeedError(f"empty frame for {self.symbol} {interval}/{period}")
                if len(df) < MIN_BARS:
                    log.warning("[%s] only %d bars returned (need >= %d) — "
                                "signals may warm up slowly", interval, len(df), MIN_BARS)
                return df
            except Exception as e:  # noqa: BLE001 — the loop must never die
                last_exc = e
                if attempt < self.max_retries:
                    backoff = min(2 ** attempt, 8)
                    log.warning("[%s] download attempt %d/%d failed (%s) — retrying in %ss",
                                interval, attempt, self.max_retries, e, backoff)
                    time.sleep(backoff)
        log.error("[%s] all %d download attempts failed: %s",
                  interval, self.max_retries, last_exc)
        return None

    # ------------------------------------------------------------------
    def _normalise(self, raw):
        if raw is None or len(raw) == 0:
            return None

        df = raw.copy()

        # yfinance >= 0.2.51 returns MultiIndex columns (field, ticker) even
        # for a single ticker. Flatten to the field level.
        if isinstance(df.columns, pd.MultiIndex):
            lvl0 = {str(c).lower() for c in df.columns.get_level_values(0)}
            level = 0 if {"open", "high", "low", "close"} <= lvl0 else 1
            df.columns = df.columns.get_level_values(level)

        df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
        required = ["open", "high", "low", "close"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise FeedError(f"missing OHLC columns {missing}; got {list(df.columns)}")

        keep = required + (["volume"] if "volume" in df.columns else [])
        df = df.loc[:, keep]
        # A column can still be duplicated if Yahoo repeats a field.
        df = df.loc[:, ~df.columns.duplicated()]

        # --- index: make it tz-aware in the exchange timezone ---
        idx = pd.to_datetime(df.index, errors="coerce")
        df.index = idx
        df = df[~df.index.isna()]
        if getattr(df.index, "tz", None) is None:
            # Naive timestamps from Yahoo intraday are UTC.
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(self.tz)

        for c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        df = df.dropna(subset=required)
        # Zero/negative prints are bad ticks, not bars.
        df = df[(df[required] > 0).all(axis=1)]
        # Impossible bars (high < low) are corrupt.
        df = df[df["high"] >= df["low"]]

        df = df[~df.index.duplicated(keep="last")].sort_index()
        if df.empty:
            return None
        return df

    # ------------------------------------------------------------------
    def _check_staleness(self, interval: str, df) -> None:
        """Warn (loudly) if the newest bar stops advancing during the session."""
        newest = df.index[-1]
        prev = self._last_bar_ts.get(interval)
        now = time.monotonic()
        if prev is not None and newest == prev:
            since = self._stale_since.setdefault(interval, now)
            stalled = now - since
            limit = INTERVAL_DELTA[interval].total_seconds() * 3
            if stalled > limit and self._in_session():
                log.warning("[%s] feed appears STALE: newest bar %s has not advanced "
                            "for %.0fs during market hours", interval, newest, stalled)
        else:
            self._last_bar_ts[interval] = newest
            self._stale_since.pop(interval, None)

    def _in_session(self) -> bool:
        from datetime import datetime, time as dtime
        now = datetime.now(self.tz)
        if now.weekday() >= 5:
            return False
        try:
            start = dtime.fromisoformat(self.session_start)
            end = dtime.fromisoformat(self.session_end)
        except ValueError:
            return True
        return start <= now.time() <= end
