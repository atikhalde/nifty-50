#!/usr/bin/env python3
"""Offline parity checks for the BSL/SSL engine + dedupe state.

Run:  python selftest.py

Validates (against hand-traced expectations derived from the Pine script):
  1. A confirmed swing HIGH opens a BSL pool and fires SELL (default mapping)
     exactly piv_len bars after the swing bar (non-repainting).
  2. A confirmed swing LOW opens an SSL pool and fires BUY.
  3. Equal highs/lows within eqTol MERGE into an existing pool and do NOT
     fire a signal.
  4. Sweeps: price through the level + close back inside removes the pool
     and sets the sweep flag.
  5. Magnet mapping (BSL->BUY, SSL->SELL) flips the signals.
  6. SL/TP math matches close -/+ atr*atr_sl*rr_target.
  7. SentState persists and never re-sends the same key.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pandas as pd

from scanner.indicators.bsl_ssl import BSLSSLParams, compute_signals, _atr
from scanner.state import SentState


def _base_frame(n: int = 200):
    idx = pd.date_range("2026-08-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    df = pd.DataFrame({
        "open": np.full(n, 95.0),
        "high": np.full(n, 100.0),
        "low": np.full(n, 90.0),
        "close": np.full(n, 95.0),
    }, index=idx)
    return df


def _spike(df, i, high=None, low=None, close=None):
    if high is not None:
        df.iloc[i, df.columns.get_loc("high")] = high
    if low is not None:
        df.iloc[i, df.columns.get_loc("low")] = low
    if close is not None:
        df.iloc[i, df.columns.get_loc("close")] = close
    return df


def test_pivot_and_sell_signal():
    df = _base_frame()
    _spike(df, 20, high=120.0)                     # swing high at bar 20
    sig = compute_signals(df, BSLSSLParams())

    # ATR must equal Wilder RMA of TR
    atr = _atr(df["high"].to_numpy(float), df["low"].to_numpy(float),
               df["close"].to_numpy(float), 14)
    assert np.allclose(sig["atr"].to_numpy(), atr, equal_nan=True), "ATR mismatch"

    # confirmation bar = 20 + 8 = 28  (NOT bar 20 -> non-repainting)
    assert not bool(sig["sell_sig"].iloc[20]), "signal must not fire on the swing bar"
    assert bool(sig["sell_sig"].iloc[28]), "SELL must fire on confirmation bar 28"
    assert sig["new_bsl_lvl"].iloc[28] == 120.0, "BSL level = pivot high"
    assert sig["new_bsl_name"].iloc[28] == "BSL-01", "first BSL pool named BSL-01"
    assert not bool(sig["buy_sig"].iloc[28]), "default mapping: BSL start -> SELL not BUY"

    # SL/TP math: sl_long = close - atr*1.2 ; tp_long = close + atr*1.2*2
    a28 = float(sig["atr"].iloc[28])
    assert abs(sig["sl_long"].iloc[28] - (95.0 - a28 * 1.2)) < 1e-9
    assert abs(sig["tp_long"].iloc[28] - (95.0 + a28 * 1.2 * 2.0)) < 1e-9
    assert abs(sig["tp_long"].iloc[28] - 95.0) == 2 * abs(95.0 - sig["sl_long"].iloc[28])

    # swing-locked: no NEW pool on bar 29
    assert not bool(sig["sell_sig"].iloc[29])
    print("  ok  pivot + SELL on confirmation bar + SL/TP math")


def test_buy_signal():
    df = _base_frame()
    _spike(df, 25, low=70.0)                       # swing low at bar 25
    sig = compute_signals(df, BSLSSLParams())
    assert bool(sig["buy_sig"].iloc[33]), "BUY must fire on confirmation bar 33"
    assert sig["new_ssl_lvl"].iloc[33] == 70.0
    assert sig["new_ssl_name"].iloc[33] == "SSL-01"
    print("  ok  pivot low + BUY on confirmation bar")


def test_equal_merge_no_signal():
    df = _base_frame()
    _spike(df, 20, high=120.0)                     # new pool @120  -> SELL at 28
    # equal-high bar: high inside eqTol (~1.7) AND close above the pool level
    # so it does NOT sweep pool @120 (sweep needs close < lvl)
    _spike(df, 30, high=120.6, close=120.2)        # within eqTol   -> merge
    _spike(df, 40, high=130.0)                     # new pool @130  -> SELL at 48
    sig = compute_signals(df, BSLSSLParams())

    assert bool(sig["sell_sig"].iloc[28])
    assert not bool(sig["sell_sig"].iloc[38]), "equal high must merge, NOT signal"
    assert np.isnan(sig["new_bsl_lvl"].iloc[38]), "no new pool on equal high"
    assert bool(sig["sell_sig"].iloc[48]), "second distinct pool must signal"
    assert sig["new_bsl_name"].iloc[48] == "BSL-02", "new pool gets next id"
    assert sig["new_bsl_lvl"].iloc[48] == 130.0
    print("  ok  equal-high merge (no signal) + distinct pool (signal)")


def test_sweep():
    df = _base_frame()
    _spike(df, 20, high=120.0)                     # BSL @120 -> SELL at 28
    _spike(df, 25, low=70.0)                       # SSL @70  -> BUY at 33
    _spike(df, 40, high=135.0, close=95.0)         # trade through both BSLs, close back in
    sig = compute_signals(df, BSLSSLParams())

    assert not bool(sig["swept_bsl"].iloc[39]), "no sweep before bar 40"
    assert bool(sig["swept_bsl"].iloc[40]), "BSL sweep on bar 40"
    # pools iterated newest->oldest; both @130? no: pools @120 only in this frame? see below
    # (bar 40 high=135 also becomes a NEW pivot confirmed at 48 -> not relevant here)
    # Bar 40's high (135) itself becomes a new pivot -> pool BSL-02 at bar 48,
    # then the bar-50 spike (high 140, close 95) sweeps BSL-02, so bar 58 -> BSL-03.
    _spike(df, 50, high=140.0)
    sig2 = compute_signals(df, BSLSSLParams())
    assert bool(sig2["sell_sig"].iloc[48]), "bar 40 high confirmed at 48 -> BSL-02"
    assert sig2["new_bsl_name"].iloc[48] == "BSL-02"
    assert bool(sig2["swept_bsl"].iloc[50]), "bar-50 spike sweeps pool @135"
    assert bool(sig2["sell_sig"].iloc[58]), "new pool after sweep"
    assert sig2["new_bsl_name"].iloc[58] == "BSL-03", "sequence continues after sweep"
    assert not bool(sig2["swept_ssl"].iloc[40]), "SSL must not be swept on bar 40 (low=90 > 70)"
    print("  ok  sweep detection + pool removal + id sequence")


def test_magnet_mapping():
    df = _base_frame()
    _spike(df, 20, high=120.0)
    _spike(df, 25, low=70.0)
    p = BSLSSLParams(sig_dir="BSL→BUY · SSL→SELL")
    sig = compute_signals(df, p)
    assert bool(sig["buy_sig"].iloc[28]), "magnet: BSL start -> BUY"
    assert not bool(sig["sell_sig"].iloc[28])
    assert bool(sig["sell_sig"].iloc[33]), "magnet: SSL start -> SELL"
    assert not bool(sig["buy_sig"].iloc[33])
    print("  ok  magnet mapping flips directions")


def test_state_dedupe():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "state.json")
        s = SentState(path)
        key = "NSE:NIFTY|5m|BUY|2026-08-31T14:35:00+05:30|24300.0"
        assert not s.already_sent(key)
        s.mark(key)
        s.set_last_evaluated("5m", "2026-08-31T14:35:00+05:30")
        s.persist()

        s2 = SentState(path)                        # reload from disk
        assert s2.already_sent(key), "dedupe must survive restart"
        assert s2.last_evaluated["5m"] == "2026-08-31T14:35:00+05:30"
    print("  ok  persistent dedupe across restarts")


def test_expiry():
    df = _base_frame(n=400)
    _spike(df, 20, high=120.0)                      # pool @120 at bar 28
    p = BSLSSLParams(pool_expiry=100)
    sig = compute_signals(df, p)
    # pool created bar 28, expires when j - 28 > 100 -> bar 129.
    # Spike close is ABOVE the pool level so the pool expires instead of sweeping.
    _spike(df, 135, high=140.0, close=125.0)        # NEW pivot (140-120=20 > eqTol)
    sig2 = compute_signals(df, p)
    assert bool(sig2["sell_sig"].iloc[143]), "after expiry the old pool is gone -> new pool signals"
    assert sig2["new_bsl_name"].iloc[143] == "BSL-02"
    print("  ok  pool expiry frees the slot for a new pool")


def test_feed_normalisation():
    """The live feed must survive every shape yfinance actually returns."""
    from scanner.data.yfinance_feed import YFinanceFeed, FeedError

    f = YFinanceFeed()
    idx = pd.date_range("2026-09-01 03:45", periods=6, freq="5min")  # naive UTC
    base = dict(Open=[100.0] * 6, High=[106.0] * 6, Low=[99.0] * 6,
                Close=[104.0] * 6, Volume=[10.0] * 6)

    # MultiIndex columns (yfinance >= 0.2.51) must flatten, index -> IST.
    mi = pd.DataFrame(base, index=idx)
    mi.columns = pd.MultiIndex.from_product([mi.columns, ["^NSEI"]])
    d = f._normalise(mi)
    assert list(d.columns) == ["open", "high", "low", "close", "volume"]
    assert str(d.index.tz) == "Asia/Kolkata", "naive Yahoo index must be UTC->IST"

    # Corrupt bars (NaN close, zero print, high<low) must be dropped.
    b = pd.DataFrame(base, index=idx)
    b.iloc[1, b.columns.get_loc("Close")] = np.nan
    b.iloc[2, b.columns.get_loc("Open")] = 0.0
    b.iloc[3, b.columns.get_loc("High")] = 1.0
    assert len(f._normalise(b)) == 3, "bad ticks must be filtered out"

    # Duplicate / unsorted timestamps must be de-duplicated and ordered.
    c = pd.concat([pd.DataFrame(base, index=idx)] * 2).sort_index(ascending=False)
    d = f._normalise(c)
    assert d.index.is_unique and d.index.is_monotonic_increasing

    # Missing OHLC must raise a typed error (caught & retried by the feed).
    try:
        f._normalise(pd.DataFrame({"Open": [1.0]}, index=idx[:1]))
        raise AssertionError("missing columns should raise")
    except FeedError:
        pass
    print("  ok  feed normalisation (MultiIndex, tz, bad ticks, dupes)")


def test_scanner_survives_bad_state():
    """A tz-naive or corrupt last_evaluated must not kill a live scan cycle."""
    import json
    from config import ScannerConfig
    from scanner.data.mock import MockFeed
    from scanner.live import LiveScanner

    with tempfile.TemporaryDirectory() as td:
        sf = os.path.join(td, "state.json")
        with open(sf, "w", encoding="utf-8") as fh:
            json.dump({"sent": {},
                       "last_evaluated": {"5m": "2026-09-01T10:00:00",   # tz-naive
                                          "1m": "not-a-date"}}, fh)      # garbage
        cfg = ScannerConfig()
        cfg.market_hours_only = False
        cfg.telegram_enabled = False
        cfg.state_file = sf
        LiveScanner(cfg, feed=MockFeed()).tick()   # must not raise
    print("  ok  scanner survives tz-naive / corrupt state")


def test_no_duplicate_and_retry_on_failure():
    """Failed delivery is retried; delivered alerts are never repeated."""
    from config import ScannerConfig
    from scanner.data.mock import MockFeed
    from scanner.live import LiveScanner
    from scanner.indicators.bsl_ssl import compute_signals
    from datetime import datetime

    with tempfile.TemporaryDirectory() as td:
        cfg = ScannerConfig()
        cfg.market_hours_only = False
        cfg.timeframes = ["5m"]
        cfg.state_file = os.path.join(td, "s.json")
        s = LiveScanner(cfg, feed=MockFeed())
        s.notifier.bots = [("bot1", "tok", "chat")]

        sent = []
        s.notifier.send = lambda text: (sent.append(text), False)[1]   # always fails
        s.tick()
        assert not s.state.sent, "failed sends must NOT be marked (they retry)"

        s.notifier.send = lambda text: (sent.append(text), True)[1]    # now succeeds
        df = s.feed.get_bars("5m")
        sig = compute_signals(df, s.params)
        now = datetime.now(s.tz)
        s.state.last_evaluated["5m"] = df.index[-6].isoformat()
        s._process_new_closed_bars("5m", df, sig, now)
        first = len(sent)
        s.state.last_evaluated["5m"] = df.index[-6].isoformat()        # replay same bars
        s._process_new_closed_bars("5m", df, sig, now)
        assert len(sent) == first, "an already-delivered alert was sent twice!"
    print("  ok  retry-on-failure + never a duplicate alert")


def test_state_ledger_is_bounded():
    with tempfile.TemporaryDirectory() as td:
        st = SentState(os.path.join(td, "p.json"))
        for i in range(SentState.MAX_KEYS + 3000):
            st.mark(f"k{i}")
        assert len(st.sent) <= SentState.MAX_KEYS
        assert st.already_sent(f"k{SentState.MAX_KEYS + 2999}"), "newest key must survive"
    print("  ok  dedupe ledger stays bounded")


def test_config_is_typo_proof():
    """A typo in .env must degrade to defaults, never crash at the open."""
    from config import ScannerConfig

    saved = {k: os.environ.get(k) for k in
             ("SCAN_INTERVAL_SEC", "SESSION_START", "TIMEFRAMES", "LOG_LEVEL")}
    try:
        os.environ.update({"SCAN_INTERVAL_SEC": "abc", "SESSION_START": "9:15",
                           "TIMEFRAMES": "1m,7m,5m", "LOG_LEVEL": "chatty"})
        c = ScannerConfig()
        assert c.scan_interval_sec == 20, "bad int must fall back to the default"
        assert c.session_start == "09:15", "'9:15' must be normalised to '09:15'"
        assert c.timeframes == ["1m", "5m"], "unsupported timeframe must be dropped"
        assert c.log_level == "INFO"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  ok  config tolerates bad .env values")


def main():
    print("BSL/SSL engine parity self-test")
    test_pivot_and_sell_signal()
    test_buy_signal()
    test_equal_merge_no_signal()
    test_sweep()
    test_magnet_mapping()
    test_expiry()
    test_state_dedupe()
    print("\nLive-market hardening checks")
    test_config_is_typo_proof()
    test_feed_normalisation()
    test_scanner_survives_bad_state()
    test_no_duplicate_and_retry_on_failure()
    test_state_ledger_is_bounded()
    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    main()
