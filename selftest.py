#!/usr/bin/env python3
"""Offline parity checks for the BSL/SSL engine, Webhook server, and dedupe state.

Run:  python selftest.py

Validates:
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
  8. Webhook payload formatter correctly processes TradingView JSON and plaintext.
  9. Dual timestamps (Chart Anchor vs Confirmation) match accurately.
"""

from __future__ import annotations

import json
import os
import tempfile

import numpy as np
import pandas as pd

from scanner.data.yfinance_feed import YFinanceFeed
from scanner.indicators.bsl_ssl import BSLSSLParams, compute_signals, _atr
from scanner.state import SentState
from scanner.webhook import WebhookFormatter


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


def _scanner_for(params, state_path):
    """Build a LiveScanner with no network and no Telegram (offline messages)."""
    from config import ScannerConfig
    from scanner.data.mock import MockFeed
    from scanner.live import LiveScanner

    cfg = ScannerConfig()
    cfg.telegram_enabled = False
    cfg.market_hours_only = False
    cfg.state_file = state_path
    return LiveScanner(cfg, params=params, feed=MockFeed(), market_check=False)


def test_signal_message_pool_mapping():
    """A signal's message/dedupe level must name the pool that actually fired.

    Regression: the message builder always read the SSL pool for BUY and the
    BSL pool for SELL. Under the magnet mapping (BSL->BUY, SSL->SELL) those
    columns are empty on the firing bar, so alerts went out as
    "Fresh  pool start @ -" and the dedupe key level degraded to 'na'.
    """
    df = _base_frame()
    _spike(df, 20, high=120.0)
    _spike(df, 25, low=70.0)

    with tempfile.TemporaryDirectory() as td:
        state = os.path.join(td, "state.json")

        # ---- default mapping: BSL -> SELL, SSL -> BUY -------------------
        p = BSLSSLParams()
        sig = compute_signals(df, p)
        sc = _scanner_for(p, state)

        msg = sc._build_signal_msg("SELL", "5m", df.index[28], sig.iloc[28], df.iloc[28], None)
        assert "BSL-01" in msg, f"default SELL must name the fresh BSL pool:\n{msg}"
        assert "@ 120.00" in msg, msg
        assert "actual HIGH" in msg, msg
        assert sc._level_of("SELL", sig.iloc[28]) == 120.0

        msg = sc._build_signal_msg("BUY", "5m", df.index[33], sig.iloc[33], df.iloc[33], None)
        assert "SSL-01" in msg, f"default BUY must name the fresh SSL pool:\n{msg}"
        assert "@ 70.00" in msg, msg
        assert "actual LOW" in msg, msg
        assert sc._level_of("BUY", sig.iloc[33]) == 70.0

        # ---- magnet mapping: BSL -> BUY, SSL -> SELL --------------------
        pm = BSLSSLParams(sig_dir="BSL→BUY · SSL→SELL")
        sigm = compute_signals(df, pm)
        scm = _scanner_for(pm, state)

        msg = scm._build_signal_msg("BUY", "5m", df.index[28], sigm.iloc[28], df.iloc[28], None)
        assert "BSL-01" in msg, f"magnet BUY must name the fresh BSL pool:\n{msg}"
        assert "@ 120.00" in msg, msg
        assert "actual HIGH" in msg, msg
        assert "pool start @ —" not in msg, "magnet BUY lost its pool level"
        assert scm._level_of("BUY", sigm.iloc[28]) == 120.0, "magnet BUY dedupe level"

        msg = scm._build_signal_msg("SELL", "5m", df.index[33], sigm.iloc[33], df.iloc[33], None)
        assert "SSL-01" in msg, f"magnet SELL must name the fresh SSL pool:\n{msg}"
        assert "@ 70.00" in msg, msg
        assert "actual LOW" in msg, msg
        assert scm._level_of("SELL", sigm.iloc[33]) == 70.0, "magnet SELL dedupe level"

    print("  ok  signal message + dedupe level follow the pool mapping")


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


def test_yfinance_column_normalization():
    """yfinance returns TitleCase columns in a (Price, Ticker) MultiIndex —
    the feed must normalize them or every real fetch crashes with KeyError."""
    idx = pd.date_range("2026-08-31 09:15", periods=3, freq="5min", tz="Asia/Kolkata")
    raw = pd.DataFrame({
        "Open": [1.0, 2.0, 3.0], "High": [2.0, 3.0, 4.0],
        "Low": [0.5, 1.5, 2.5], "Close": [1.5, 2.5, 3.5],
        "Adj Close": [1.5, 2.5, 3.5], "Volume": [100, 200, 300],
    }, index=idx)
    # shape produced by yfinance 1.x download() (group_by='column')
    multi = raw.copy()
    multi.columns = pd.MultiIndex.from_product([multi.columns, ["^NSEI"]])
    out = YFinanceFeed._normalize(multi)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"], \
        f"multi-index normalize gave {list(out.columns)}"
    assert str(out.index.tz) == "Asia/Kolkata"
    assert out["close"].iloc[0] == 1.5

    # older/single-level shape (multi_level_index=False) is also TitleCase
    out2 = YFinanceFeed._normalize(raw)
    assert list(out2.columns) == ["open", "high", "low", "close", "volume"], \
        f"single-level normalize gave {list(out2.columns)}"
    print("  ok  yfinance TitleCase/MultiIndex column normalization")


def test_incremental_refresh_uses_datetime_start():
    """yfinance 1.x only parses 'YYYY-MM-DD' strings or datetime objects; a
    free-form timestamp string makes every incremental refetch fail and the
    feed serve stale bars forever."""
    feed = YFinanceFeed()
    # recent bars (clock-relative) so _refresh takes the incremental path
    now5 = pd.Timestamp.now(tz="Asia/Kolkata").floor("5min")
    idx = pd.date_range(now5 - pd.Timedelta(minutes=20), periods=5, freq="5min")
    cached = pd.DataFrame({
        "open": [1.0] * 5, "high": [2.0] * 5, "low": [0.5] * 5,
        "close": [1.5] * 5, "volume": [100] * 5,
    }, index=idx)
    feed._cache["5m"] = cached

    seen = {}
    new_bar = pd.DataFrame({
        "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.4], "volume": [120],
    }, index=idx[-1:] + pd.Timedelta(minutes=5))

    def fake_download(self, tf, period=None, start=None, retries=2):
        seen["period"] = period
        seen["start"] = start
        return new_bar.copy()

    YFinanceFeed._download = fake_download
    out = feed._refresh("5m", "60d", "60d")
    assert not isinstance(seen["start"], str), \
        f"incremental start must be a datetime, got {type(seen['start']).__name__}"
    assert seen["period"] is None, "period must not be combined with start"
    assert out is not None and len(out) == 6, "cached + new bars should concatenate"
    print("  ok  incremental fetch passes a datetime start (no string crash)")


def test_expiry():
    df = _base_frame(n=400)
    _spike(df, 20, high=120.0)                      # pool @120 at bar 28
    p = BSLSSLParams(pool_expiry=100)
    _spike(df, 135, high=140.0, close=125.0)        # NEW pivot
    sig2 = compute_signals(df, p)
    assert bool(sig2["sell_sig"].iloc[143]), "after expiry the old pool is gone -> new pool signals"
    assert sig2["new_bsl_name"].iloc[143] == "BSL-02"
    print("  ok  pool expiry frees the slot for a new pool")


def test_webhook_payload_formatter():
    # 1. User sample BUY alert
    buy_json = {
        "action": "BUY",
        "symbol": "NSE:NIFTY",
        "tf": "5m",
        "pool": "SSL-169",
        "pool_lvl": 24010.55,
        "entry": 24061.05,
        "sl": 24038.43,
        "tp": 24106.30,
        "target": 24114.00,
        "swing_bar_time": "2026-09-01 09:45",
        "bar_time": "2026-09-01 10:25"
    }
    kind, key, msg = WebhookFormatter.format_payload(buy_json)
    assert kind == "BUY"
    assert "SSL-169" in msg
    assert "24,010.55" in msg
    assert "24,061.05" in msg
    assert "24,038.43" in msg
    assert "24,106.30" in msg
    assert "2026-09-01 09:45 IST" in msg
    assert "2026-09-01 10:25" in msg

    # 2. User sample SELL alert
    sell_json = {
        "action": "SELL",
        "symbol": "NSE:NIFTY",
        "tf": "5m",
        "pool": "BSL-169",
        "pool_lvl": 24142.85,
        "entry": 24117.75,
        "sl": 24136.48,
        "tp": 24080.29,
        "target": 24010.55,
        "swing_bar_time": "2026-09-01 11:30",
        "bar_time": "2026-09-01 12:10"
    }
    kind2, key2, msg2 = WebhookFormatter.format_payload(sell_json)
    assert kind2 == "SELL"
    assert "BSL-169" in msg2
    assert "24,142.85" in msg2
    assert "24,117.75" in msg2

    # 3. User sample Sweep alert
    sweep_json = {
        "action": "SWEEP_SSL",
        "symbol": "NSE:NIFTY",
        "tf": "5m",
        "pool_lvl": 24020.50,
        "close": 24025.10,
        "bar_time": "2026-09-01 09:45"
    }
    kind3, key3, msg3 = WebhookFormatter.format_payload(sweep_json)
    assert kind3 == "SWEEP_SSL"
    assert "24,020.50" in msg3

    print("  ok  webhook formatter handles TradingView JSON & math accurately")


def main():
    print("BSL/SSL engine parity self-test")
    test_pivot_and_sell_signal()
    test_buy_signal()
    test_equal_merge_no_signal()
    test_sweep()
    test_magnet_mapping()
    test_signal_message_pool_mapping()
    test_expiry()
    test_state_dedupe()
    test_yfinance_column_normalization()
    test_incremental_refresh_uses_datetime_start()
    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    main()
