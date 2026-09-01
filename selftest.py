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

from scanner.indicators.bsl_ssl import BSLSSLParams, compute_signals, _atr, _pivots
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


def test_pivot_tie_semantics_match_tradingview():
    """TradingView ta.pivothigh/pivotlow: ties allowed with OLDER bars, strict
    against NEWER bars (the NEWER twin of equal extremes is the pivot).
    A 'strict both sides' port drops every flat double-top/bottom signal."""
    # twin equal highs 3 bars apart -> ONE pivot, on the NEWER twin (bar 13),
    # confirmed at 13 + 8 = 21
    df = _base_frame()
    _spike(df, 10, high=120.05)
    _spike(df, 13, high=120.05)
    sig = compute_signals(df, BSLSSLParams(piv_len=8))
    assert not bool(sig["sell_sig"].iloc[18]), "older twin shadowed — no pivot at 10+8"
    assert bool(sig["sell_sig"].iloc[21]), "newer twin is the pivot (TV behaviour)"
    assert sig["new_bsl_lvl"].iloc[21] == 120.05

    # full-array parity against a literal reference implementation of TV's rule
    rng = np.random.default_rng(5)
    h = np.round(rng.normal(100, 1, 400) / 0.05) * 0.05   # 0.05 ticks -> many ties
    l = h - np.round(np.abs(rng.normal(0, 0.8, 400)) / 0.05) * 0.05
    ph, pl = _pivots(h, l, 8)

    def ref(arr, piv, is_high):
        out = np.full(len(arr), np.nan)
        for j in range(2 * piv, len(arr)):
            w = arr[j - 2 * piv: j + 1]                    # oldest -> newest
            last = len(w) - 1 - (w[::-1].argmax() if is_high else w[::-1].argmin())
            if last == piv:                                # newest extreme == candidate
                out[j] = arr[j - piv]
        return out

    assert np.array_equal(np.isnan(ph), np.isnan(ref(h, 8, True))) and \
        np.allclose(ph, ref(h, 8, True), equal_nan=True), "pivothigh must match TV exactly"
    assert np.array_equal(np.isnan(pl), np.isnan(ref(l, 8, False))) and \
        np.allclose(pl, ref(l, 8, False), equal_nan=True), "pivotlow must match TV exactly"
    print("  ok  pivot tie semantics identical to TradingView (twins + random tie-prone series)")


def _scanner_for_msg_test(tmpdir, sig_dir):
    from config import ScannerConfig
    from scanner.live import LiveScanner
    cfg = ScannerConfig()
    cfg.state_file = os.path.join(tmpdir, "state.json")
    cfg.telegram_enabled = False
    return LiveScanner(cfg, params=BSLSSLParams(sig_dir=sig_dir), feed=object())


def test_magnet_alert_message():
    """In magnet mode the engine fired correct signals already, but the alert
    TEXT quoted the opposite (non-existent) pool and the wrong swing side."""
    df = _base_frame()
    _spike(df, 20, high=120.0)                      # BSL start -> BUY under magnet
    p = BSLSSLParams(sig_dir="BSL→BUY · SSL→SELL")
    sig = compute_signals(df, p)
    ts, row, bar = sig.index[28], sig.iloc[28], df.iloc[28]
    assert bool(row["buy_sig"])
    with tempfile.TemporaryDirectory() as td:
        scanner = _scanner_for_msg_test(td, "BSL→BUY · SSL→SELL")
        msg = scanner._build_signal_msg("BUY", "5m", ts, row, bar)
        assert "BSL-01" in msg and "120.00" in msg, f"magnet BUY must quote the BSL pool:\n{msg}"
        assert "actual HIGH" in msg
        lvl = scanner._level_of("BUY", row, scanner.params.magnet)
        assert lvl == 120.0
    print("  ok  magnet-mode alert content (pool side, level, swing text, dedupe level)")


def test_eod_grace_window():
    """Bars closing exactly at session end (15:30) must still be processed
    same-day: the scan gate stays open for EOD_GRACE_MIN extra minutes."""
    from config import ScannerConfig
    from scanner.live import LiveScanner
    from zoneinfo import ZoneInfo
    from datetime import datetime
    with tempfile.TemporaryDirectory() as td:
        cfg = ScannerConfig()
        cfg.state_file = os.path.join(td, "state.json")
        cfg.telegram_enabled = False
        cfg.eod_grace_min = 6
        sc = LiveScanner(cfg, params=BSLSSLParams(), feed=object())
        tz = ZoneInfo("Asia/Kolkata")
        assert sc._market_open(datetime(2026, 9, 1, 14, 0, tzinfo=tz))       # mid-session
        assert sc._market_open(datetime(2026, 9, 1, 15, 35, tzinfo=tz))      # grace window
        assert not sc._market_open(datetime(2026, 9, 1, 15, 40, tzinfo=tz))  # past grace
        assert not sc._market_open(datetime(2026, 9, 1, 8, 0, tzinfo=tz))    # pre-open
        assert not sc._market_open(datetime(2026, 9, 5, 12, 0, tzinfo=tz))   # Saturday
    print("  ok  EOD grace window flushes last-bar signals same-day")


def test_partial_telegram_failure_retries_only_failed_bot():
    """bot1 ok + bot2 fails -> next cycle retries bot2 ONLY (no duplicate on
    bot1), and the alert is marked fully sent once every bot has it."""
    from config import ScannerConfig
    from scanner.live import LiveScanner

    class StubNotifier:
        bots = [("bot1", "t1", "c1"), ("bot2", "t2", "c2")]
        bot_names = ["bot1", "bot2"]

        def __init__(self):
            self.calls = []
            self.fail_bot2 = True

        def send(self, text, only=None):
            targets = only or self.bot_names
            self.calls.append(list(targets))
            return {b: not (b == "bot2" and self.fail_bot2) for b in targets}

    df = _base_frame()
    _spike(df, 20, high=120.0)
    sig = compute_signals(df, BSLSSLParams())
    ts, row, bar = sig.index[28], sig.iloc[28], df.iloc[28]
    assert bool(row["sell_sig"])

    with tempfile.TemporaryDirectory() as td:
        cfg = ScannerConfig()
        cfg.state_file = os.path.join(td, "state.json")
        sc = LiveScanner(cfg, params=BSLSSLParams(), feed=object())
        stub = StubNotifier()
        sc.notifier = stub
        cfg.sweep_alerts = False

        sc._emit_bar("5m", ts, row, bar)
        assert stub.calls == [["bot1", "bot2"]]
        key = f"NSE:NIFTY|5m|SELL|{ts.isoformat()}|120.0"
        assert key in sc.state.sent or f"{key}@bot1" in sc.state.sent
        assert not sc.state.already_sent(key), "not fully delivered -> no master mark"
        assert sc.state.already_sent(key + "@bot1")
        assert not sc.state.already_sent(key + "@bot2")

        stub.fail_bot2 = False
        sc._emit_bar("5m", ts, row, bar)
        assert stub.calls[-1] == ["bot2"], "retry must target only the failed bot"
        assert sc.state.already_sent(key), "fully delivered -> master key marked"

        sc._emit_bar("5m", ts, row, bar)     # third pass: nothing to send
        assert len(stub.calls) == 2
    print("  ok  partial Telegram failure: retry only the failed bot, then mark sent once")


def main():
    print("BSL/SSL engine parity self-test")
    test_pivot_and_sell_signal()
    test_buy_signal()
    test_equal_merge_no_signal()
    test_sweep()
    test_magnet_mapping()
    test_expiry()
    test_state_dedupe()
    test_pivot_tie_semantics_match_tradingview()
    test_magnet_alert_message()
    test_eod_grace_window()
    test_partial_telegram_failure_retries_only_failed_bot()
    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    main()
