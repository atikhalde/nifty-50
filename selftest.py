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
  9. Dual timestamps (Chart Anchor vs Execution Bar) match accurately.
 10. Multi-Speed TIER 2: fast_piv_len=3 fires 62% faster than piv_len=8.
 11. Multi-Speed TIER 3: standard piv_len=8 remains intact for macro tracking.
 12. Multi-Speed TIER 1: instant sweep trades fire on the sweep candle (0-bar
     lag) with wick SL and 1:2 R:R TP.
 13. LiveScanner wiring: new closed bars produce (and dedupe) real alerts for
     all tiers — guards against a silent-death regression in the alert path.
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

        msg = sc._build_signal_msg("SELL", "5m", df.index[28], sig.iloc[28], df.iloc[28], df.index[20])
        assert "BSL-01" in msg, f"default SELL must name the fresh BSL pool:\n{msg}"
        assert "@ 120.00" in msg, msg
        assert "actual HIGH" in msg, msg
        assert "Chart Anchor (Swing High): 2026-08-01 10:55 IST" in msg
        assert sc._level_of("SELL", sig.iloc[28]) == 120.0

        msg = sc._build_signal_msg("BUY", "5m", df.index[33], sig.iloc[33], df.iloc[33], df.index[25])
        assert "SSL-01" in msg, f"default BUY must name the fresh SSL pool:\n{msg}"
        assert "@ 70.00" in msg, msg
        assert "actual LOW" in msg, msg
        assert "Chart Anchor (Swing Low): 2026-08-01 11:20 IST" in msg
        assert sc._level_of("BUY", sig.iloc[33]) == 70.0

        # ---- magnet mapping: BSL -> BUY, SSL -> SELL --------------------
        pm = BSLSSLParams(sig_dir="BSL→BUY · SSL→SELL")
        sigm = compute_signals(df, pm)
        scm = _scanner_for(pm, state)

        msg = scm._build_signal_msg("BUY", "5m", df.index[28], sigm.iloc[28], df.iloc[28], df.index[20])
        assert "BSL-01" in msg, f"magnet BUY must name the fresh BSL pool:\n{msg}"
        assert "@ 120.00" in msg, msg
        assert "actual HIGH" in msg, msg
        assert "pool start @ —" not in msg, "magnet BUY lost its pool level"
        assert scm._level_of("BUY", sigm.iloc[28]) == 120.0, "magnet BUY dedupe level"

        msg = scm._build_signal_msg("SELL", "5m", df.index[33], sigm.iloc[33], df.iloc[33], df.index[25])
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


def test_fast_pivot_62pct_faster():
    """TIER 2: fast_piv_len=3 confirms 5 bars earlier than piv_len=8 (62.5%)."""
    df = _base_frame()
    _spike(df, 20, high=120.0)
    p = BSLSSLParams()
    assert p.piv_len == 8 and p.fast_piv_len == 3
    faster = (p.piv_len - p.fast_piv_len) / p.piv_len
    assert abs(faster - 0.625) < 1e-9, f"expected 62.5% faster, got {faster:.1%}"

    sig = compute_signals(df, p)
    assert not bool(sig["fast_sell_sig"].iloc[20]), "fast signal must not fire on the swing bar"
    assert bool(sig["fast_sell_sig"].iloc[23]), "FAST SELL must fire on bar 20+3=23"
    assert not bool(sig["sell_sig"].iloc[23]), "STANDARD must still wait for 8-bar confirmation"
    assert bool(sig["sell_sig"].iloc[28]), "STANDARD SELL still fires on bar 20+8=28"
    assert sig["fast_new_bsl_lvl"].iloc[23] == 120.0
    assert sig["fast_new_bsl_name"].iloc[23] == "FAST-BSL"
    # 5m: 3 bars = 15 minutes; 1m: 3 bars = 3 minutes
    assert (df.index[23] - df.index[20]) == pd.Timedelta(minutes=15)
    print("  ok  TIER 2 fast pivot (3-bar, 62% faster) + TIER 3 standard intact")


def test_fast_buy_and_magnet():
    df = _base_frame()
    _spike(df, 25, low=70.0)
    sig = compute_signals(df, BSLSSLParams())
    assert bool(sig["fast_buy_sig"].iloc[28]), "FAST BUY on bar 25+3=28"
    assert bool(sig["buy_sig"].iloc[33]), "STANDARD BUY still on bar 25+8=33"

    pm = BSLSSLParams(sig_dir="BSL→BUY · SSL→SELL")
    sigm = compute_signals(df, pm)
    assert bool(sigm["fast_sell_sig"].iloc[28]), "magnet: fast SSL start -> FAST SELL"
    assert not bool(sigm["fast_buy_sig"].iloc[28])
    print("  ok  TIER 2 fast BUY + magnet flip")


def test_instant_sweep_trade_0_bar_lag():
    """TIER 1: execute on the sweep candle close, wick SL, 1:2 R:R TP."""
    df = _base_frame()
    _spike(df, 20, high=120.0)                     # BSL @120 confirmed at 28
    _spike(df, 25, low=70.0)                       # SSL @70 confirmed at 33
    _spike(df, 40, high=135.0, close=95.0)         # BSL sweep
    _spike(df, 45, low=50.0, close=95.0)           # SSL sweep
    sig = compute_signals(df, BSLSSLParams())

    assert bool(sig["swept_bsl"].iloc[40])
    assert bool(sig["inst_sell_sig"].iloc[40]), "INSTANT SELL must fire ON the BSL sweep bar (0-lag)"
    assert not bool(sig["inst_sell_sig"].iloc[39]), "must not fire before the sweep"
    assert sig["inst_sl_short"].iloc[40] == 135.0, "tight wick SL = sweep candle high"
    # entry 95, risk 40, 1:2 TP = 95 - 80 = 15
    assert abs(sig["inst_tp_short"].iloc[40] - (95.0 - 2.0 * (135.0 - 95.0))) < 1e-9
    assert sig["inst_pool_lvl"].iloc[40] == 120.0

    assert bool(sig["swept_ssl"].iloc[45])
    assert bool(sig["inst_buy_sig"].iloc[45]), "INSTANT BUY must fire ON the SSL sweep bar (0-lag)"
    assert sig["inst_sl_long"].iloc[45] == 50.0, "tight wick SL = sweep candle low"
    assert abs(sig["inst_tp_long"].iloc[45] - (95.0 + 2.0 * (95.0 - 50.0))) < 1e-9
    assert sig["inst_pool_lvl"].iloc[45] == 70.0
    print("  ok  TIER 1 instant sweep trade (0-bar lag, wick SL, 1:2 R:R)")


def test_multi_speed_telegram_timestamps():
    """All 3 tiers expose Chart Anchor vs Execution Bar."""
    df = _base_frame()
    _spike(df, 20, high=120.0)
    _spike(df, 40, high=135.0, close=95.0)
    p = BSLSSLParams()
    sig = compute_signals(df, p)

    with tempfile.TemporaryDirectory() as td:
        sc = _scanner_for(p, os.path.join(td, "state.json"))

        msg_std = sc._build_signal_msg(
            "SELL", "5m", df.index[28], sig.iloc[28], df.iloc[28], df.index[20], speed="standard")
        assert "· STANDARD" in msg_std
        assert "Chart Anchor (Swing High): 2026-08-01 10:55 IST" in msg_std
        assert "Execution Bar: 2026-08-01 11:35 IST" in msg_std
        assert "Swing confirmed 8 bars after actual HIGH" in msg_std

        msg_fast = sc._build_signal_msg(
            "SELL", "5m", df.index[23], sig.iloc[23], df.iloc[23], df.index[20], speed="fast")
        assert "· FAST" in msg_fast
        assert "FAST-BSL" in msg_fast
        assert "Chart Anchor (Swing High): 2026-08-01 10:55 IST" in msg_fast
        assert "Execution Bar: 2026-08-01 11:10 IST" in msg_fast  # 3 x 5m after 10:55
        assert "Fast swing confirmed 3 bars after actual HIGH" in msg_fast
        assert "62% faster" in msg_fast

        msg_inst = sc._build_instant_msg(
            "SELL", "5m", df.index[40], sig.iloc[40], df.iloc[40], df.index[40])
        assert "INSTANT SWEEP SELL" in msg_inst
        assert "TIER 1" in msg_inst
        assert "(wick)" in msg_inst
        assert "0-bar lag" in msg_inst
        # Chart Anchor and Execution Bar are the same candle
        assert "Chart Anchor (Sweep High): 2026-08-01 12:35 IST" in msg_inst
        assert "Execution Bar: 2026-08-01 12:35 IST" in msg_inst
        assert "SL: 135.00" in msg_inst or "SL: 135.00" in msg_inst.replace(",", "")
    print("  ok  dual timestamps (Chart Anchor vs Execution Bar) for all 3 tiers")


def test_webhook_multi_speed_tiers():
    """Webhook formatter emits distinct copy + dual timestamps per speed tier."""
    std = {
        "action": "BUY", "speed": "standard", "piv_len": 8,
        "symbol": "NSE:NIFTY", "tf": "5m", "pool": "SSL-01",
        "pool_lvl": 24010.55, "entry": 24061.05, "sl": 24038.43, "tp": 24106.30,
        "target": 24114.00, "swing_bar_time": "2026-09-01 09:45",
        "bar_time": "2026-09-01 10:25",
    }
    kind, key, msg = WebhookFormatter.format_payload(std)
    assert kind == "BUY"
    assert "STANDARD" in msg
    assert "Chart Anchor (Swing Low): 2026-09-01 09:45 IST" in msg
    assert "Execution Bar: 2026-09-01 10:25 IST" in msg
    assert "Swing confirmed 8 bars after actual LOW" in msg

    fast = {
        "action": "BUY", "speed": "fast", "piv_len": 3,
        "symbol": "NSE:NIFTY", "tf": "5m", "pool": "FAST-SSL",
        "pool_lvl": 24010.55, "entry": 24040.00, "sl": 24020.00, "tp": 24080.00,
        "target": 24114.00, "swing_bar_time": "2026-09-01 10:10",
        "bar_time": "2026-09-01 10:25",
    }
    kind2, key2, msg2 = WebhookFormatter.format_payload(fast)
    assert kind2 == "FAST_BUY"
    assert "FAST" in msg2
    assert "FAST-SSL" in msg2
    assert "Chart Anchor (Swing Low): 2026-09-01 10:10 IST" in msg2
    assert "Execution Bar: 2026-09-01 10:25 IST" in msg2
    assert "Fast swing confirmed 3 bars after actual LOW" in msg2
    assert "62% faster" in msg2
    assert key2 != key

    inst = {
        "action": "INST_BUY", "speed": "instant", "piv_len": 0,
        "symbol": "NSE:NIFTY", "tf": "5m", "pool": "SSL-01",
        "pool_lvl": 24020.50, "entry": 24025.10, "sl": 24000.00, "tp": 24075.30,
        "swing_bar_time": "2026-09-01 09:45", "bar_time": "2026-09-01 09:45",
    }
    kind3, key3, msg3 = WebhookFormatter.format_payload(inst)
    assert kind3 == "INST_BUY"
    assert "INSTANT SWEEP BUY" in msg3
    assert "TIER 1" in msg3
    assert "(wick)" in msg3
    assert "0-bar lag" in msg3
    assert "Chart Anchor (Sweep Low): 2026-09-01 09:45 IST" in msg3
    assert "Execution Bar: 2026-09-01 09:45 IST" in msg3
    print("  ok  webhook formatter distinct alerts for Standard / Fast / Instant")


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


def test_live_scanner_wiring_offline():
    """End-to-end alert-path wiring through LiveScanner (offline, MockFeed).

    Regression guard: production previously called _emit_bar with a wrong
    signature; tick() swallowed the TypeError every cycle, so the scanner
    LOOKED alive but never sent a single alert. This test calls the exact
    method tick() calls and requires >0 emitted+deduped alerts.
    """
    from config import ScannerConfig
    from scanner.data.mock import MockFeed
    from scanner.live import LiveScanner

    with tempfile.TemporaryDirectory() as td:
        cfg = ScannerConfig()
        cfg.market_hours_only = False
        cfg.telegram_enabled = False          # dry-run: logged + marked sent
        cfg.state_file = os.path.join(td, "state.json")
        cfg.log_file = os.path.join(td, "scanner.log")

        sc = LiveScanner(cfg, feed=MockFeed())
        df = sc.feed.get_bars("5m")

        # pretend bars up to index 99 were evaluated in an earlier session
        sc.state.set_last_evaluated("5m", df.index[99].isoformat())
        sig = compute_signals(df, sc.params)
        now = df.index[-1] + pd.Timedelta(minutes=5)   # every bar is closed

        # The exact method tick() invokes — must not raise.
        sc._process_new_closed_bars("5m", df, sig, now)
        assert len(sc.state.sent) > 0, "live path emitted ZERO alerts — wiring broken"

        kinds = {k.split("|")[2] for k in sc.state.sent}
        later = sig.iloc[100:]
        assert kinds & {"BUY", "SELL", "SWEEP_SSL", "SWEEP_BSL"}, \
            f"no standard/sweep alerts emitted (kinds={kinds})"
        if bool(later["fast_buy_sig"].any()) or bool(later["fast_sell_sig"].any()):
            assert kinds & {"FAST_BUY", "FAST_SELL"}, "TIER 2 engine fired but no FAST alert"
        if bool(later["inst_buy_sig"].any()) or bool(later["inst_sell_sig"].any()):
            assert kinds & {"INST_BUY", "INST_SELL"}, "TIER 1 engine fired but no INST alert"

        # Re-walking already-evaluated bars must never duplicate an alert.
        n_before = len(sc.state.sent)
        sc._process_new_closed_bars("5m", df, sig, now)
        assert len(sc.state.sent) == n_before, "duplicate alerts on re-walk"

        # Public entry point must complete cleanly as well.
        sc.tick()
    print("  ok  live wiring: standard/FAST/INST alerts emitted + strictly deduped")


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
    test_webhook_payload_formatter()
    test_fast_pivot_62pct_faster()
    test_fast_buy_and_magnet()
    test_instant_sweep_trade_0_bar_lag()
    test_multi_speed_telegram_timestamps()
    test_webhook_multi_speed_tiers()
    test_live_scanner_wiring_offline()
    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    main()
