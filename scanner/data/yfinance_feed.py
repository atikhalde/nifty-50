"""yfinance data feed for the BSL/SSL scanner (NSE:NIFTY, ``^NSEI``).

Responsibilities
----------------
* Pull enough history to warm up the pool state so it converges to the
  TradingView chart (~7 trading days of 1m bars, ~60 days of 5m bars), then
  refresh incrementally on every scan.
* Return a clean OHLC ``DataFrame`` with **lowercase** columns
  (``open, high, low, close, volume``) and a **timezone-aware** index in the
  configured session timezone (IST by default) — ``scanner/live.py`` relies on
  the index being tz-aware for its closed-bar / dedupe arithmetic.
* Filter intraday bars to the NSE cash session (``09:15``–``15:30`` IST) so the
  bars line up 1:1 with TradingView.

yfinance is free and unofficial; Yahoo can throttle or drop connections, so
every network call is wrapped and failures degrade gracefully (an empty frame),
never crash the scan loop.
"""

from __future__ import annotations

import logging
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

# How much history to request per timeframe (warm-up + live refresh in one go).
# Yahoo limits: 1m -> max 7 days back, 5m/15m -> max 60 days back.
_TF_PLAN: dict[str, dict[str, str]] = {
    "1m": {"interval": "1m", "period": "7d"},
    "2m": {"interval": "2m", "period": "60d"},
    "5m": {"interval": "5m", "period": "60d"},
    "15m": {"interval": "15m", "period": "60d"},
    "30m": {"interval": "30m", "period": "60d"},
    "60m": {"interval": "60m", "period": "60d"},
    "1h": {"interval": "60m", "period": "60d"},
}

# One bar's duration per timeframe — used by live.py to decide when a bar is
# fully closed (``index + delta <= now``).
INTERVAL_DELTA: dict[str, pd.Timedelta] = {
    "1m": pd.Timedelta(minutes=1),
    "2m": pd.Timedelta(minutes=2),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "60m": pd.Timedelta(minutes=60),
    "1h": pd.Timedelta(minutes=60),
}


class YFinanceFeed:
    def __init__(
        self,
        symbol: str = "^NSEI",
        tz: str = "Asia/Kolkata",
        session_start: str = "09:15",
        session_end: str = "15:30",
        session_filter: bool = True,
    ):
        self.symbol = symbol
        self.tzname = tz
        self.tz = ZoneInfo(tz)
        self.session_start = dtime.fromisoformat(session_start)
        self.session_end = dtime.fromisoformat(session_end)
        self.session_filter = session_filter

    # ------------------------------------------------------------------
    def get_bars(self, tf: str) -> pd.DataFrame:
        """Return an OHLC frame for ``tf`` (tz-aware IST index, session-filtered).

        On any error (network, throttle, empty response) an **empty** DataFrame
        is returned so the caller can simply skip this cycle.
        """
        plan = _TF_PLAN.get(tf)
        if plan is None:
            log.warning("Unsupported timeframe %r — skipping", tf)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        try:
            import yfinance as yf

            raw = yf.download(
                self.symbol,
                interval=plan["interval"],
                period=plan["period"],
                auto_adjust=False,
                progress=False,
                threads=False,
            )
        except Exception:
            log.exception("yfinance download failed for %s %s", self.symbol, tf)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        return self._normalize(raw)

    # ------------------------------------------------------------------
    def _normalize(self, raw: pd.DataFrame) -> pd.DataFrame:
        cols = ["open", "high", "low", "close", "volume"]
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=cols)

        df = raw.copy()

        # yfinance may return MultiIndex columns (field, ticker) for a single
        # symbol — flatten to the field level.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.columns = [str(c).strip().lower() for c in df.columns]
        # Deduplicate any repeated column labels (keep first occurrence).
        df = df.loc[:, ~pd.Index(df.columns).duplicated()]

        missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
        if missing:
            log.error("yfinance frame missing columns %s (got %s)", missing, list(df.columns))
            return pd.DataFrame(columns=cols)

        if "volume" not in df.columns:
            df["volume"] = 0.0

        df = df[["open", "high", "low", "close", "volume"]]

        # --- timezone: make the index tz-aware in the session timezone ---
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is None:
            # Yahoo intraday is normally tz-aware; if not, assume UTC then convert.
            idx = idx.tz_localize("UTC")
        idx = idx.tz_convert(self.tz)
        df.index = idx

        # --- clean up ---
        df = df.apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()

        # --- restrict to the NSE cash session so bars match TradingView ---
        if self.session_filter and len(df) > 0:
            wk = df.index.weekday < 5  # Mon–Fri
            t = df.index.time
            in_sess = (t >= self.session_start) & (t <= self.session_end)
            df = df[wk & in_sess]

        return df
