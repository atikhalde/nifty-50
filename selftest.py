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

        # pending queue: survives restart, cleared once delivered
        s2.add_pending("k2", "hello", "2026-08-31T14:36:00+05:30")
        s2.persist()
        s3 = SentState(path)
        assert "k2" in s3.pending and s3.pending["k2"]["text"] == "hello"
        s3.mark("k2")
        assert "k2" not in s3.pending and s3.already_sent("k2")
        # marking as sent blocks re-queueing
        s3.add_pending("k2", "hello", "2026-08-31T14:37:00+05:30")
        assert "k2" not in s3.pending
    print("  ok  persistent dedupe across restarts")


def test_feed_normalization():
    """YFinanceFeed._normalize: session filter, dedupe, tz, NaN, MultiIndex."""
    from scanner.data.yfinance_feed import YFinanceFeed

    feed = YFinanceFeed(symbol="^NSEI", tz="Asia/Kolkata")
    idx = pd.DatetimeIndex([
        "2026-08-31 09:14",   # pre-open -> dropped
        "2026-08-31 09:15",   # kept
        "2026-08-31 12:00",   # kept (duplicated below)
        "2026-08-31 12:00",   # duplicate -> keep last
        "2026-08-31 13:00",   # NaN close -> dropped
        "2026-08-31 15:29",   # kept (last real 1m bar)
        "2026-08-31 15:30",   # stray auction bar -> dropped
    ]).tz_localize("Asia/Kolkata")
    raw = pd.DataFrame({
        "Open":  [1, 2, 3, 3.5, 5, 6, 7],
        "High":  [1, 2, 3, 3.5, 5, 6, 7],
        "Low":   [1, 2, 3, 3.5, 5, 6, 7],
        "Close": [1, 2, 3, 3.5, np.nan, 6, 7],
        "Volume": [0] * 7,
    }, index=idx)

    df = feed._normalize(raw)
    assert list(df.columns)[:4] == ["open", "high", "low", "close"], "lower-case OHLC"
    assert len(df) == 3, f"expected 3 bars after filtering, got {len(df)}"
    assert df.index.is_unique and df.index.is_monotonic_increasing
    assert float(df["close"].iloc[1]) == 3.5, "duplicate timestamp must keep the LATEST value"
    assert str(df.index.tz) == "Asia/Kolkata"

    # MultiIndex columns (yf.download style) also normalise
    raw2 = raw.copy()
    raw2.columns = pd.MultiIndex.from_product([raw.columns, ["^NSEI"]])
    df2 = feed._normalize(raw2)
    assert list(df2.columns)[:4] == ["open", "high", "low", "close"]

    # tz-naive input gets localised (UTC) then converted
    raw3 = raw.copy()
    raw3.index = raw3.index.tz_localize(None)
    df3 = feed._normalize(raw3)
    assert df3 is None or str(df3.index.tz) == "Asia/Kolkata"
    print("  ok  feed normalisation (session filter, dedupe, tz, NaN, MultiIndex)")


class _FakeNotifier:
    """Notifier double: scripted results, records every send."""
    def __init__(self, results=None):
        self.enabled = True
        self.bots = [("fake", "t", "c")]
        self.sent: list[str] = []
        self._results = list(results or [])

    def send(self, text):
        self.sent.append(text)
        return self._results.pop(0) if self._results else True


def _make_live_scanner(td, feed, notifier, tf="5m"):
    from config import ScannerConfig
    from scanner.live import LiveScanner

    cfg = ScannerConfig()
    cfg.timeframes = [tf]
    cfg.market_hours_only = False
    cfg.max_alert_age_min = 10**6          # mock bars are in the past
    cfg.state_file = os.path.join(td, "state.json")
    cfg.telegram_enabled = True
    sc = LiveScanner(cfg, params=BSLSSLParams(), feed=feed)
    sc.notifier = notifier
    return sc


def test_live_pipeline_end_to_end():
    """MockFeed -> engine -> dedupe -> notifier, no duplicates across ticks."""
    from scanner.data.mock import MockFeed

    with tempfile.TemporaryDirectory() as td:
        notifier = _FakeNotifier()
        sc = _make_live_scanner(td, MockFeed(), notifier)

        sc.tick()                                   # baseline: no history spam
        assert notifier.sent == [], "first tick must only set the baseline"

        for _ in range(120):                        # stream the rest
            sc.tick()
        n_alerts = len(notifier.sent)
        assert n_alerts > 0, "streaming synthetic data must produce alerts"
        assert len(set(notifier.sent)) == n_alerts, "no duplicate alert texts"

        for _ in range(5):                          # feed exhausted -> no repeats
            sc.tick()
        assert len(notifier.sent) == n_alerts, "no alert may ever be re-sent"

        for m in notifier.sent:
            assert ("BUY SIGNAL" in m or "SELL SIGNAL" in m or "SWEPT" in m)
            assert "NSE:NIFTY" in m
    print(f"  ok  end-to-end pipeline: {n_alerts} unique alerts, zero duplicates")


def test_failed_delivery_retry():
    """A failed send must be retried on later ticks — never lost, never doubled."""
    from scanner.data.mock import MockFeed

    with tempfile.TemporaryDirectory() as td:
        # every send fails at first
        notifier = _FakeNotifier(results=[False] * 500)
        sc = _make_live_scanner(td, MockFeed(), notifier)
        sc.tick()                                   # baseline
        for _ in range(40):
            sc.tick()
        assert len(sc.state.pending) > 0, "failed alerts must be queued as pending"
        n_pending = len(sc.state.pending)
        pending_keys = set(sc.state.pending)

        # deliveries recover
        notifier._results = []                      # -> always True now
        before = len(notifier.sent)
        sc.tick()
        assert len(sc.state.pending) == 0, "pending queue must drain once delivery works"
        assert len(notifier.sent) >= before + n_pending
        for k in pending_keys:
            assert sc.state.already_sent(k), "retried alert must be marked sent"
    print("  ok  failed deliveries queued and retried (never lost)")


def test_stale_bar_guard():
    """After a restart with an old state file, stale bars must NOT alert."""
    from scanner.data.mock import MockFeed

    with tempfile.TemporaryDirectory() as td:
        notifier = _FakeNotifier()
        sc = _make_live_scanner(td, MockFeed(), notifier)
        sc.cfg.max_alert_age_min = 1                # everything mock is older
        sc.tick()                                   # baseline
        for _ in range(60):
            sc.tick()
        assert notifier.sent == [], "stale bars (older than the guard) must not alert"
        assert len(sc.state.pending) == 0
    print("  ok  stale-bar guard suppresses restart/outage catch-up spam")


def test_expiry():
    df = _base_frame(n=400)
    _spike(df, 20, high=120.0)                      # pool @120 at bar 28
    p = BSLSSLParams(pool_expiry=100)
    _ = compute_signals(df, p)   # first pass (pool @120 created, later expires)
    # pool created bar 28, expires when j - 28 > 100 -> bar 129.
    # Spike close is ABOVE the pool level so the pool expires instead of sweeping.
    _spike(df, 135, high=140.0, close=125.0)        # NEW pivot (140-120=20 > eqTol)
    sig2 = compute_signals(df, p)
    assert bool(sig2["sell_sig"].iloc[143]), "after expiry the old pool is gone -> new pool signals"
    assert sig2["new_bsl_name"].iloc[143] == "BSL-02"
    print("  ok  pool expiry frees the slot for a new pool")


def main():
    print("BSL/SSL engine parity self-test")
    test_pivot_and_sell_signal()
    test_buy_signal()
    test_equal_merge_no_signal()
    test_sweep()
    test_magnet_mapping()
    test_expiry()
    test_state_dedupe()
    test_feed_normalization()
    test_live_pipeline_end_to_end()
    test_failed_delivery_retry()
    test_stale_bar_guard()
    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    main()
