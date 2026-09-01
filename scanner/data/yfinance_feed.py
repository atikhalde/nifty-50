"""Yahoo Finance bar feed for the scanner.

Provides a warm-up window (enough history for the pool state to converge with
a TradingView chart) plus an incremental cache so only new bars are fetched on
each scan cycle. Intraday bars are filtered to the NSE session
(09:15-15:30 IST) so they match TradingView.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TLS hardening
# ---------------------------------------------------------------------------
# yfinance prefers the curl_cffi backend, whose bundled BoringSSL frequently
# drops the handshake to query2.finance.yahoo.com in sandboxed/CI networks
# ("BoringSSL SSL_connect ... SSL_ERROR_SYSCALL"). Forcing the plain
# requests/OpenSSL backend makes the connection succeed there.
#
# yfinance reads this env var at IMPORT time (yfinance/_http.py), so it must be
# set before the first `import yfinance` — hence at module import time here.
# YF_USE_CURL_CFFI is also set for older/newer variants of the same knob.
os.environ.setdefault("YF_DISABLE_CURL_CFFI", "1")
os.environ.setdefault("YF_USE_CURL_CFFI", "0")

_YF_CONFIGURED = False


def _patch_requests_exceptions() -> None:
    """Add curl_cffi-only exception names to ``requests.exceptions``.

    With the curl_cffi backend disabled, yfinance still references
    ``requests.exceptions.DNSError`` (a curl_cffi-only class), which raises
    ``AttributeError: module 'requests.exceptions' has no attribute 'DNSError'``
    and turns every successful download into an "empty response". Alias the
    missing names onto requests' own ConnectionError so the handler works.
    """
    import requests

    for name in ("DNSError", "CurlError", "ImpersonateError"):
        if not hasattr(requests.exceptions, name):
            setattr(requests.exceptions, name, requests.exceptions.ConnectionError)


def _import_yfinance():
    """Import yfinance with the curl_cffi backend disabled (idempotent)."""
    global _YF_CONFIGURED
    _patch_requests_exceptions()
    import yfinance as yf

    if not _YF_CONFIGURED:
        _YF_CONFIGURED = True
        try:
            from yfinance import _http as yf_http
            if getattr(yf_http, "HAS_CURL_CFFI", False):
                log.warning("yfinance is using the curl_cffi backend — TLS "
                            "handshake failures to Yahoo are likely")
        except Exception:  # noqa: BLE001 - internal module, best effort
            pass
        try:
            # Bound the internal retry budget too (kwarg absent on old versions)
            yf.set_config(retries=3)
        except Exception:  # noqa: BLE001
            log.debug("yf.set_config unavailable")
    return yf


# Interval -> pandas timedelta
INTERVAL_DELTA = {
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
}
_PRELOAD = {
    # yfinance only serves ~7 days of 1m bars (and 5m bars with a 60d period)
    "1m": ("7d", "7d"),
    "5m": ("60d", "60d"),
}

# Canonical lowercase column names we need, plus all the aliases yfinance has
# used over the years (it always returned TitleCase prices; never lowercase).
_COL_ALIASES = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    # adjusted variants — never used by the engine, dropped
    "adj open": None,
    "adj high": None,
    "adj low": None,
    "adj close": None,
}


class YFinanceFeed:
    def __init__(self, symbol: str = "^NSEI", tz: str = "Asia/Kolkata",
                 session_start: str = "09:15", session_end: str = "15:30"):
        self.symbol = symbol
        self.tz = ZoneInfo(tz)
        self.session_start = session_start
        self.session_end = session_end
        self._cache: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    def get_bars(self, tf: str) -> pd.DataFrame | None:
        """Return cached+refreshed OHLC bars for `tf` (index tz-aware, tz=self.tz).

        Always returns the FULL warm-up window — the engine needs the history
        to converge its pool state (pivot confirmation needs 8 bars each side).
        Lookback mode slices the *alerting* window in live.py, not here.
        """
        period, preload = _PRELOAD.get(tf, ("60d", "60d"))
        df = self._refresh(tf, period, preload)
        if df is None or df.empty:
            return None
        return self._session_filter(df)

    # ------------------------------------------------------------------
    def _refresh(self, tf: str, period: str, preload: str) -> pd.DataFrame | None:
        cached = self._cache.get(tf)
        if cached is None:
            df = self._download(tf, period=preload)      # warm-up window
        else:
            last = cached.index.max()
            if datetime.now(self.tz) - last > pd.Timedelta(minutes=7):
                # re-download the whole window — yfinance may have revised bars
                df = self._download(tf, period=period)
            else:
                # Incremental fetch: only the last ~10 minutes (yfinance needs a
                # tz-aware datetime here, NOT a free-form string — its parser
                # only accepts YYYY-MM-DD or datetime objects).
                start = last - pd.Timedelta(minutes=10)
                df = self._download(tf, start=start)
                if df is not None and not df.empty:
                    df = pd.concat([cached, df])
        if df is None or df.empty:
            # Fetch failed (e.g. Yahoo TLS handshake drop). Serve the last good
            # bars instead of raising — the scanner must never crash on a
            # transient network failure; it will simply emit no new signals.
            if cached is not None and not cached.empty:
                log.warning("yfinance unavailable (tf=%s) — serving %d cached bars",
                            tf, len(cached))
            return cached
        df = self._normalize(df)
        self._cache[tf] = df
        return df

    def _download(self, tf: str, period: str | None = None,
                  start: pd.Timestamp | datetime | None = None,
                  retries: int = 5) -> pd.DataFrame | None:
        """Download OHLCV bars from yfinance.

        `period` (e.g. "7d"/"60d") is used for the warm-up fetch; `start` (a
        tz-aware datetime) is used for the incremental fetch. NEVER pass a
        free-form timestamp string to yfinance — yfinance 1.x only accepts
        'YYYY-MM-DD' strings or datetime objects (see _parse_user_dt), and a
        bad `start` silently makes the whole download return an empty frame.
        """
        yf = _import_yfinance()
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                if start is not None:
                    if period is not None:
                        # yfinance treats period as the window length measured
                        # from `start`; the scanner wants "everything from start
                        # to now", so drop period when start is given.
                        period = None
                    df = yf.download(
                        self.symbol, interval=tf,
                        start=start, progress=False, auto_adjust=False,
                        threads=False,
                    )
                else:
                    df = yf.download(
                        self.symbol, interval=tf, period=period,
                        progress=False, auto_adjust=False,
                        threads=False,
                    )
                if df is not None and not df.empty:
                    return df
                last_err = RuntimeError("empty yfinance response")
            except Exception as e:                      # noqa: BLE001
                last_err = e
            if attempt < retries - 1:
                # exponential backoff (1s, 2s, 4s, 8s ...), capped at 30s —
                # Yahoo's TLS drops are transient and usually clear on retry.
                delay = min(2.0 ** attempt, 30.0)
                log.debug("yfinance fetch failed (tf=%s, attempt %d/%d), "
                          "retrying in %.0fs: %s",
                          tf, attempt + 1, retries, delay, last_err)
                time.sleep(delay)
        if last_err is not None:
            log.warning("yfinance fetch failed (tf=%s): %s", tf, last_err)
        return None

    @staticmethod
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        """Normalize any yfinance OHLCV frame.

        yfinance returns TitleCase columns
        (Open/High/Low/Close/Adj Close/Volume) and, with its default
        `multi_level_index=True`, wraps them in a (Price, Ticker) MultiIndex.
        Older/other versions may return single-level or (Ticker, Price)
        columns. This handles all of them and returns a frame with lowercase
        open/high/low/close/volume on a tz-aware Asia/Kolkata index.
        """
        if df is None or df.empty:
            return df

        if isinstance(df.columns, pd.MultiIndex):
            # pick the level that actually contains price names
            wanted = {c for c, mapped in _COL_ALIASES.items() if mapped}
            lvl = None
            for i in range(df.columns.nlevels):
                vals = {str(v).strip().lower() for v in df.columns.get_level_values(i)}
                if vals & wanted:
                    lvl = i
                    break
            if lvl is None:
                lvl = 0
            df = df.copy()
            df.columns = df.columns.get_level_values(lvl)

        df = df.copy()
        df.columns = [str(c).strip().lower() for c in df.columns]

        # keep only known price/volume aliases (drops Adj Close etc.),
        # then map them to the canonical lowercase names
        df = df[[c for c in df.columns if _COL_ALIASES.get(c) is not None]]
        df = df.rename(columns={c: _COL_ALIASES[c] for c in df.columns})

        missing = [c for c in ("open", "high", "low", "close", "volume")
                   if c not in df.columns]
        if missing:
            raise ValueError(f"yfinance response missing columns {missing}")
        df = df[["open", "high", "low", "close", "volume"]].copy()

        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Kolkata")
        else:
            df.index = df.index.tz_convert("Asia/Kolkata")
        return df

    def _session_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """Keep only bars whose timestamp falls inside the NSE session.

        NSE session 09:15-15:30 IST: a 5m bar starting at 15:25 ends at 15:30 and is the last valid bar.
        A bar starting at 15:30 itself is outside session (it would end at 15:35).
        So we filter on start time: session_start <= time < session_end.
        This matches TradingView's session filter for NSE.
        """
        s_h, s_m = map(int, self.session_start.split(":"))
        e_h, e_m = map(int, self.session_end.split(":"))
        s_t = dtime(s_h, s_m)
        e_t = dtime(e_h, e_m)

        def _inside(ts):
            t = ts.time()
            return s_t <= t < e_t

        mask = df.index.to_series().apply(_inside)
        return df[mask]
