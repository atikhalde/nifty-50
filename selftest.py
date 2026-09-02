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
 10. Multi-Speed TIER 2: fast_piv_len=3 fires 25% faster than piv_len=4 by default.
 11. Multi-Speed TIER 3: standard piv_len=4 remains intact for macro tracking.
 12. Multi-Speed TIER 1: instant sweep trades fire on the sweep candle (0-bar
     lag) with wick SL and 1:2 R:R TP.
 13. LiveScanner wiring: new closed bars produce (and dedupe) real alerts for
     all tiers — guards against a silent-death regression in the alert path.
 14. Feed: yfinance TitleCase/MultiIndex columns normalise, incremental
     refresh passes a datetime start, bad ticks/dupes are dropped.
"""

from __future__ import annotations

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

    # confirmation bar = 20 + 4 = 24  (NOT bar 20 -> non-repainting)
    assert not bool(sig["sell_sig"].iloc[20]), "signal must not fire on the swing bar"
    assert bool(sig["sell_sig"].iloc[24]), "SELL must fire on confirmation bar 24"
    assert sig["new_bsl_lvl"].iloc[24] == 120.0, "BSL level = pivot high"
    assert sig["new_bsl_name"].iloc[24] == "BSL-01", "first BSL pool named BSL-01"
    assert not bool(sig["buy_sig"].iloc[24]), "default mapping: BSL start -> SELL not BUY"

    # SL/TP math: sl_long = close - atr*1.2 ; tp_long = close + atr*1.2*2
    a24 = float(sig["atr"].iloc[24])
    assert abs(sig["sl_long"].iloc[24] - (95.0 - a24 * 1.2)) < 1e-9
    assert abs(sig["tp_long"].iloc[24] - (95.0 + a24 * 1.2 * 2.0)) < 1e-9
    assert abs(sig["tp_long"].iloc[24] - 95.0) == 2 * abs(95.0 - sig["sl_long"].iloc[24])

    # swing-locked: no NEW pool on bar 29
    assert not bool(sig["sell_sig"].iloc[29])
    print("  ok  pivot + SELL on confirmation bar + SL/TP math")


def test_buy_signal():
    df = _base_frame()
    _spike(df, 25, low=70.0)                       # swing low at bar 25
    sig = compute_signals(df, BSLSSLParams())
    assert bool(sig["buy_sig"].iloc[29]), "BUY must fire on confirmation bar 29"
    assert sig["new_ssl_lvl"].iloc[29] == 70.0
    assert sig["new_ssl_name"].iloc[29] == "SSL-01"
    print("  ok  pivot low + BUY on confirmation bar")


def test_equal_merge_no_signal():
    df = _base_frame()
    _spike(df, 20, high=120.0)                     # new pool @120  -> SELL at 24
    _spike(df, 30, high=120.6, close=120.2)        # within eqTol   -> merge
    _spike(df, 40, high=130.0)                     # new pool @130  -> SELL at 44
    sig = compute_signals(df, BSLSSLParams())

    assert bool(sig["sell_sig"].iloc[24])
    assert not bool(sig["sell_sig"].iloc[34]), "equal high must merge, NOT signal"
    assert np.isnan(sig["new_bsl_lvl"].iloc[34]), "no new pool on equal high"
    assert bool(sig["sell_sig"].iloc[44]), "second distinct pool must signal"
    assert sig["new_bsl_name"].iloc[44] == "BSL-02", "new pool gets next id"
    assert sig["new_bsl_lvl"].iloc[44] == 130.0
    print("  ok  equal-high merge (no signal) + distinct pool (signal)")


def test_sweep():
    df = _base_frame()
    _spike(df, 20, high=120.0)                     # BSL @120 -> SELL at 24
    _spike(df, 25, low=70.0)                       # SSL @70  -> BUY at 29
    _spike(df, 40, high=135.0, close=95.0)         # trade through both BSLs, close back in
    sig = compute_signals(df, BSLSSLParams())

    assert not bool(sig["swept_bsl"].iloc[39]), "no sweep before bar 40"
    assert bool(sig["swept_bsl"].iloc[40]), "BSL sweep on bar 40"
    
    _spike(df, 50, high=140.0)
    sig2 = compute_signals(df, BSLSSLParams())
    assert bool(sig2["sell_sig"].iloc[44]), "bar 40 high confirmed at 44 -> BSL-02"
    assert sig2["new_bsl_name"].iloc[44] == "BSL-02"
    assert bool(sig2["swept_bsl"].iloc[50]), "bar-50 spike sweeps pool @135"
    assert bool(sig2["sell_sig"].iloc[54]), "new pool after sweep"
    assert sig2["new_bsl_name"].iloc[54] == "BSL-03", "sequence continues after sweep"
    assert not bool(sig2["swept_ssl"].iloc[40]), "SSL must not be swept on bar 40 (low=90 > 70)"
    print("  ok  sweep detection + pool removal + id sequence")


def test_magnet_mapping():
    df = _base_frame()
    _spike(df, 20, high=120.0)
    _spike(df, 25, low=70.0)
    p = BSLSSLParams(sig_dir="BSL→BUY · SSL→SELL")
    sig = compute_signals(df, p)
    assert bool(sig["buy_sig"].iloc[24]), "magnet: BSL start -> BUY"
    assert not bool(sig["sell_sig"].iloc[24])
    assert bool(sig["sell_sig"].iloc[29]), "magnet: SSL start -> SELL"
    assert not bool(sig["buy_sig"].iloc[29])
    print("  ok  magnet mapping flips directions")


def test_fast_tier_signals():
    """TIER 2: fast_piv_len=3 confirms the same swing 1 bar earlier (25%
    faster than the piv_len=4 default) without disturbing the standard tier."""
    df = _base_frame()
    _spike(df, 20, high=120.0)                     # swing high at bar 20
    p = BSLSSLParams()                             # piv_len=4, fast_piv_len=3
    sig = compute_signals(df, p)

    assert bool(sig["fast_bsl_start"].iloc[23]), "fast pivot confirmed at 20+3"
    assert sig["fast_new_bsl_lvl"].iloc[23] == 120.0
    assert bool(sig["fast_sell_sig"].iloc[23]), "default fade: fast BSL -> FAST SELL"
    assert not bool(sig["sell_sig"].iloc[23]), "standard tier must NOT fire at bar 23"
    assert not bool(sig["fast_sell_sig"].iloc[20]), "fast tier is non-repainting too"
    assert bool(sig["sell_sig"].iloc[24]), "standard tier still intact at bar 28"
    assert round(100.0 * (p.piv_len - p.fast_piv_len) / p.piv_len) == 25

    # magnet flips the fast tier as well
    sigm = compute_signals(df, BSLSSLParams(sig_dir="BSL→BUY · SSL→SELL"))
    assert bool(sigm["fast_buy_sig"].iloc[23]), "magnet: fast BSL -> FAST BUY"
    assert not bool(sigm["fast_sell_sig"].iloc[23])

    # master switch disables the tier without touching the others
    sig_off = compute_signals(df, BSLSSLParams(fast_signals=False))
    assert not bool(sig_off["fast_sell_sig"].iloc[23])
    assert bool(sig_off["sell_sig"].iloc[24]), "standard tier must survive FAST_SIGNALS=false"
    print("  ok  TIER 2 fast swing fires 25% earlier (independent of TIER 3)")


def test_fast_magnet_target_uses_standard_book():
    """Fast magnet labels use Pine's standard fresh-pool target, not the
    fast pivot or an unrelated nearest pool."""
    df = _base_frame()
    _spike(df, 20, high=120.0)  # standard/fast confirmations are 24/23
    p = BSLSSLParams(sig_dir="BSL→BUY · SSL→SELL")
    sig = compute_signals(df, p)
    with tempfile.TemporaryDirectory() as td:
        sc = _scanner_for(p, os.path.join(td, "state.json"))
        # At fast confirmation 23 no standard pool exists yet: Pine buyTgt is na.
        msg = sc._build_signal_msg("BUY", "5m", df.index[23], sig.iloc[23], df.iloc[23], df.index[20], speed="fast")
        assert "Nearest pool:" not in msg and "Target pool:" not in msg, msg

    # Use equal strengths to force both books to create on one bar. Pine's
    # fast label then targets the standard fresh BSL, not the fast level.
    p2 = BSLSSLParams(piv_len=3, fast_piv_len=3, sig_dir="BSL→BUY · SSL→SELL")
    sig2 = compute_signals(df, p2)
    with tempfile.TemporaryDirectory() as td:
        sc2 = _scanner_for(p2, os.path.join(td, "state.json"))
        msg = sc2._build_signal_msg("BUY", "5m", df.index[23], sig2.iloc[23], df.iloc[23], df.index[20], speed="fast")
        assert "Target pool: 120.00" in msg, msg
    print("  ok  fast magnet target follows the standard Pine pool book")


def test_instant_sweep_tier():
    """TIER 1: instant trades fire ON the sweep candle (0-bar lag) with the
    wick as SL and a rr_target multiple of the wick risk as TP."""
    df = _base_frame()
    _spike(df, 20, high=120.0)                     # BSL @120 confirmed at 28
    _spike(df, 40, high=135.0, close=95.0)         # sweep: high>lvl, close<lvl
    p = BSLSSLParams()
    sig = compute_signals(df, p)

    assert not bool(sig["inst_sell_sig"].iloc[39]), "no instant signal before the sweep"
    assert bool(sig["swept_bsl"].iloc[40])
    assert bool(sig["inst_sell_sig"].iloc[40]), "instant SELL on the sweep candle itself"
    assert sig["inst_sl_short"].iloc[40] == 135.0, "SL = sweep candle wick (high)"
    assert abs(sig["inst_tp_short"].iloc[40] - (95.0 - 2.0 * 40.0)) < 1e-9, "1:2 R:R"
    assert sig["inst_pool_lvl"].iloc[40] == 120.0, "anchor pool = swept BSL"

    # SSL sweep -> INSTANT BUY
    df2 = _base_frame()
    _spike(df2, 25, low=70.0)                      # SSL @70 confirmed at 33
    _spike(df2, 45, low=55.0, close=95.0)          # sweep: low<lvl, close>lvl
    sig2 = compute_signals(df2, p)
    assert bool(sig2["swept_ssl"].iloc[45])
    assert bool(sig2["inst_buy_sig"].iloc[45]), "instant BUY on SSL reclaim"
    assert sig2["inst_sl_long"].iloc[45] == 55.0, "SL = sweep candle wick (low)"
    assert abs(sig2["inst_tp_long"].iloc[45] - (95.0 + 2.0 * 40.0)) < 1e-9

    # master switch
    sig_off = compute_signals(df, BSLSSLParams(instant_sweep_trades=False))
    assert not bool(sig_off["inst_sell_sig"].iloc[40])
    assert bool(sig_off["swept_bsl"].iloc[40]), "sweep flag must stay on without the tier"
    print("  ok  TIER 1 instant sweep trade (0-bar lag, wick SL, 1:2 R:R)")


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

        msg = sc._build_signal_msg("SELL", "5m", df.index[24], sig.iloc[24], df.iloc[24], df.index[20])
        assert "BSL-01" in msg, f"default SELL must name the fresh BSL pool:\n{msg}"
        assert "@ 120.00" in msg, msg
        assert "actual HIGH" in msg, msg
        assert "Chart Anchor (Swing High): 2026-08-01 10:55 IST" in msg
        assert sc._level_of("SELL", sig.iloc[24]) == 120.0

        msg = sc._build_signal_msg("BUY", "5m", df.index[29], sig.iloc[29], df.iloc[29], df.index[25])
        assert "SSL-01" in msg, f"default BUY must name the fresh SSL pool:\n{msg}"
        assert "@ 70.00" in msg, msg
        assert "actual LOW" in msg, msg
        assert "Chart Anchor (Swing Low): 2026-08-01 11:20 IST" in msg
        assert sc._level_of("BUY", sig.iloc[29]) == 70.0

        # ---- magnet mapping: BSL -> BUY, SSL -> SELL --------------------
        pm = BSLSSLParams(sig_dir="BSL→BUY · SSL→SELL")
        sigm = compute_signals(df, pm)
        scm = _scanner_for(pm, state)

        msg = scm._build_signal_msg("BUY", "5m", df.index[24], sigm.iloc[24], df.iloc[24], df.index[20])
        assert "BSL-01" in msg, f"magnet BUY must name the fresh BSL pool:\n{msg}"
        assert "@ 120.00" in msg, msg
        assert "actual HIGH" in msg, msg
        assert "pool start @ —" not in msg, "magnet BUY lost its pool level"
        assert scm._level_of("BUY", sigm.iloc[24]) == 120.0, "magnet BUY dedupe level"

        msg = scm._build_signal_msg("SELL", "5m", df.index[29], sigm.iloc[29], df.iloc[29], df.index[25])
        assert "SSL-01" in msg, f"magnet SELL must name the fresh SSL pool:\n{msg}"
        assert "@ 70.00" in msg, msg
        assert "actual LOW" in msg, msg
        assert scm._level_of("SELL", sigm.iloc[29]) == 70.0, "magnet SELL dedupe level"

    print("  ok  signal message + dedupe level follow the pool mapping")


def test_webhook_formatter():
    """TradingView JSON (all speed tiers + sweeps) and plaintext payloads must
    map to the right alert kind, a stable dedupe key, and a formatted message."""
    base = {
        "symbol": "NSE:NIFTY", "tf": "5m",
        "bar_time": "2026-09-01 10:05", "swing_bar_time": "2026-09-01 09:45",
        "pool": "SSL-06", "pool_lvl": 24189.27, "entry": 24228.74,
        "sl": 24156.67, "tp": 24372.88, "target": 24411.91,
    }

    # --- standard BUY ---
    kind, key, msg = WebhookFormatter.format_payload({**base, "action": "BUY"})
    assert kind == "BUY", kind
    assert key == "TRADINGVIEW|NSE:NIFTY|5m|BUY|2026-09-01 10:05|24189.27", key
    assert "🟢 BUY SIGNAL — NSE:NIFTY (5m) · STANDARD" in msg, msg
    assert "SSL-06" in msg and "24,189.27" in msg
    assert "Chart Anchor (Swing Low): 2026-09-01 09:45 IST" in msg, msg
    assert "Swing confirmed 4 bars after actual LOW" in msg, msg

    # Magnet BUY comes from BSL, therefore its anchor is HIGH.
    magnet = {**base, "action": "BUY", "pool": "BSL-06", "pool_side": "BSL",
              "target": 24189.27}
    kind, _, magnet_msg = WebhookFormatter.format_payload(magnet)
    assert kind == "BUY"
    assert "Chart Anchor (Swing High)" in magnet_msg
    assert "Swing confirmed 4 bars after actual HIGH" in magnet_msg
    assert "Target pool: 24,189.27" in magnet_msg

    # --- fast BUY (TIER 2) ---
    kind, key, msg = WebhookFormatter.format_payload({**base, "action": "BUY", "speed": "fast"})
    assert kind == "FAST_BUY", kind
    assert "· FAST" in msg and "25% faster" in msg, msg

    # --- instant BUY (TIER 1) ---
    kind, key, msg = WebhookFormatter.format_payload(
        {**base, "action": "INSTANT_BUY", "pool": "SSL-02", "pool_lvl": 24020.5})
    assert kind == "INST_BUY", kind
    assert "⚡ INSTANT SWEEP BUY" in msg and "(wick)" in msg, msg

    # --- standard SELL ---
    kind, key, msg = WebhookFormatter.format_payload(
        {**base, "action": "SELL", "pool": "BSL-06", "pool_side": "BSL"}
    )
    assert kind == "SELL", kind
    assert "🔴 SELL SIGNAL" in msg and "Chart Anchor (Swing High)" in msg, msg

    # --- sweeps (must not be mistaken for BUY/SELL entries) ---
    kind, key, msg = WebhookFormatter.format_payload(
        {"action": "SWEEP_BSL", "symbol": "NSE:NIFTY", "tf": "5m",
         "bar_time": "2026-09-01 11:35", "pool_lvl": 24114.0, "close": 24061.0})
    assert kind == "SWEEP_BSL", kind
    assert "🧹 BSL SWEPT (Bearish Rejection)" in msg, msg
    kind, key, msg = WebhookFormatter.format_payload(
        {"action": "SSL_SWEPT", "symbol": "NSE:NIFTY", "tf": "5m",
         "bar_time": "2026-09-01 09:45", "pool_lvl": 24020.5, "close": 24061.0})
    assert kind == "SWEEP_SSL", kind
    assert "🧹 SSL SWEPT (Bullish Reclaim)" in msg, msg

    # --- plaintext alert ---
    kind, key, msg = WebhookFormatter.format_payload("raw plain alert text")
    assert kind == "PLAINTEXT" and msg == "raw plain alert text"
    assert key.startswith("TRADINGVIEW|NSE:NIFTY|PLAINTEXT|"), key

    # --- unknown JSON falls back to a stable digest key (no randomised hash) ---
    kind, key, msg = WebhookFormatter.format_payload({"foo": "bar"})
    assert kind == "GENERIC", kind
    parts = key.split("|")
    assert parts[3] == "GENERIC" and len(parts[-1]) == 16, \
        f"generic key must end in a 16-char sha256 digest: {key}"
    _, key2, _ = WebhookFormatter.format_payload({"foo": "bar"})
    assert key == key2, "generic key must be stable across calls"

    print("  ok  webhook formatter (standard/fast/instant/sweep/plaintext)")


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

        relation = s3.record_source_event(
            "NSE:NIFTY|5m|BUY|2026-08-31 14:35", "TRADINGVIEW",
            {"pool_lvl": 24300.0, "entry": 24310.0, "sl": 24290.0, "tp": 24350.0},
        )
        assert relation["status"] == "new"
        relation = s3.record_source_event(
            "NSE:NIFTY|5m|BUY|2026-08-31 14:35", "YAHOO",
            {"pool_lvl": 24300.0, "entry": 24310.0, "sl": 24290.0, "tp": 24350.0},
        )
        assert relation["status"] == "confirmed"
        relation = s3.record_source_event(
            "NSE:NIFTY|5m|BUY|2026-08-31 14:35", "YAHOO",
            {"pool_lvl": 24300.0, "entry": 24311.0, "sl": 24290.0, "tp": 24350.0},
        )
        assert relation["status"] == "same_source"
        s3.persist()
        s4 = SentState(path)
        assert "NSE:NIFTY|5m|BUY|2026-08-31 14:35" in s4.source_events

        # Two long-lived source processes must merge rather than overwrite
        # each other's sent keys when they persist the shared ledger.
        s5, s6 = SentState(path), SentState(path)
        assert s5.claim("concurrent-a")
        s5.mark("concurrent-a")
        s5.persist()
        assert s6.claim("concurrent-b")
        s6.mark("concurrent-b")
        s6.persist()
        s7 = SentState(path)
        assert s7.already_sent("concurrent-a") and s7.already_sent("concurrent-b")
    print("  ok  persistent dedupe + cross-source correlation")


def test_yfinance_column_normalization():
    """yfinance returns TitleCase columns in a (Price, Ticker) MultiIndex —
    the feed must normalize them or every real fetch crashes with KeyError."""
    idx = pd.date_range("2026-08-31 09:15", periods=3, freq="5min", tz="Asia/Kolkata")
    raw = pd.DataFrame({
        "Open": [1.0, 2.0, 3.0], "High": [2.0, 3.0, 4.0],
        "Low": [0.5, 1.5, 2.5], "Close": [1.5, 2.5, 3.5],
        "Adj Close": [1.5, 2.5, 3.5], "Volume": [100, 200, 300],
    }, index=idx)
    # shape produced by yfinance download() (group_by='column')
    multi = raw.copy()
    multi.columns = pd.MultiIndex.from_product([multi.columns, ["^NSEI"]])
    out = YFinanceFeed()._normalise(multi)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"], \
        f"multi-index normalize gave {list(out.columns)}"
    assert str(out.index.tz) == "Asia/Kolkata"
    assert out["close"].iloc[0] == 1.5

    # older/single-level shape (multi_level_index=False) is also TitleCase
    out2 = YFinanceFeed()._normalise(raw)
    assert list(out2.columns) == ["open", "high", "low", "close", "volume"], \
        f"single-level normalize gave {list(out2.columns)}"
    print("  ok  yfinance TitleCase/MultiIndex column normalization")


def test_incremental_refresh_uses_datetime_start():
    """yfinance only parses 'YYYY-MM-DD' strings or datetime objects; a
    free-form timestamp string makes every incremental refetch fail and the
    feed serve stale bars forever."""
    import time as _time

    feed = YFinanceFeed()
    # recent bars (clock-relative) so _refresh takes the incremental path
    now5 = pd.Timestamp.now(tz="Asia/Kolkata").floor("5min")
    idx = pd.date_range(now5 - pd.Timedelta(minutes=20), periods=5, freq="5min")
    cached = pd.DataFrame({
        "open": [1.0] * 5, "high": [2.0] * 5, "low": [0.5] * 5,
        "close": [1.5] * 5, "volume": [100] * 5,
    }, index=idx)
    feed._cache["5m"] = (_time.monotonic() - 999.0, cached)

    seen = {}
    new_bar = pd.DataFrame({
        "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.4], "volume": [120],
    }, index=idx[-1:] + pd.Timedelta(minutes=5))

    original_download = YFinanceFeed._download

    def fake_download(self, interval, period=None, start=None, expect_few=False):
        seen["period"] = period
        seen["start"] = start
        return new_bar.copy()

    YFinanceFeed._download = fake_download
    try:
        out = feed._refresh("5m", "1mo")
    finally:
        YFinanceFeed._download = original_download

    assert seen.get("start") is not None, "incremental refresh must pass a start"
    assert not isinstance(seen["start"], str), \
        f"incremental start must be a datetime, got {type(seen['start']).__name__}"
    assert seen["period"] is None, "period must not be combined with start"
    assert out is not None and len(out) == 6, "cached + new bars should concatenate"
    assert out.index.is_unique and out.index.is_monotonic_increasing
    assert out["close"].iloc[-1] == 1.4, "the freshly downloaded bar must win"
    print("  ok  incremental fetch passes a datetime start (no string crash)")


def test_expiry():
    df = _base_frame(n=400)
    _spike(df, 20, high=120.0)                      # pool @120 at bar 28
    p = BSLSSLParams(pool_expiry=100)
    _spike(df, 135, high=140.0, close=125.0)        # NEW pivot
    sig2 = compute_signals(df, p)
    assert bool(sig2["sell_sig"].iloc[139]), "after expiry the old pool is gone -> new pool signals"
    assert sig2["new_bsl_name"].iloc[139] == "BSL-02"
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
    test_fast_tier_signals()
    test_fast_magnet_target_uses_standard_book()
    test_instant_sweep_tier()
    test_signal_message_pool_mapping()
    test_expiry()
    test_state_dedupe()
    test_webhook_formatter()
    print("\nLive-market hardening checks")
    test_config_is_typo_proof()
    test_yfinance_column_normalization()
    test_incremental_refresh_uses_datetime_start()
    test_feed_normalisation()
    test_scanner_survives_bad_state()
    test_no_duplicate_and_retry_on_failure()
    test_state_ledger_is_bounded()
    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    main()
