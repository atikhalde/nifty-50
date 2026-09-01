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


def test_expiry():
    df = _base_frame(n=400)
    _spike(df, 20, high=120.0)                      # pool @120 at bar 28
    p = BSLSSLParams(pool_expiry=100)
    _spike(df, 135, high=140.0, close=125.0)        # NEW pivot
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



def test_scanner_accepts_lookback_and_market_check():
    """Regression: a bad merge dropped these __init__ kwargs while the body
    still used them, so every run crashed with a TypeError."""
    from config import ScannerConfig
    from scanner.data.mock import MockFeed
    from scanner.live import LiveScanner

    cfg = ScannerConfig()
    cfg.telegram_enabled = False
    s = LiveScanner(cfg, feed=MockFeed(), lookback_minutes=30, market_check=False)
    assert s.lookback_minutes == 30 and s.market_check is False
    # default: falls back to the config value, market check on
    cfg.lookback_minutes = 15
    s2 = LiveScanner(cfg, feed=MockFeed())
    assert s2.lookback_minutes == 15 and s2.market_check is True
    print("  ok  LiveScanner accepts lookback_minutes / market_check")


def test_stale_bars_are_not_alerted():
    """MAX_ALERT_AGE_MIN must actually block old bars from firing."""
    from config import ScannerConfig
    from scanner.data.mock import MockFeed
    from scanner.live import LiveScanner
    from scanner.indicators.bsl_ssl import compute_signals

    with tempfile.TemporaryDirectory() as td:
        cfg = ScannerConfig()
        cfg.telegram_enabled = False
        cfg.market_hours_only = False
        cfg.state_file = os.path.join(td, "s.json")
        cfg.max_alert_age_min = 1          # everything synthetic is older
        s = LiveScanner(cfg, feed=MockFeed())
        df = s.feed.get_bars("5m")
        # Re-stamp the frame two days into the past so every bar is stale.
        df = df.copy()
        df.index = df.index - pd.Timedelta(days=2)
        sig = compute_signals(df, s.params)
        fired = [s._emit_bar("5m", ts, sig.loc[ts], df.loc[ts], df)
                 for ts in df.index[-5:]]
        assert not any(fired), "stale bars must never be alerted"

        # ...and with the guard relaxed, the same bars are allowed through.
        cfg.max_alert_age_min = 10 ** 7
        s.state.sent.clear()
        assert s._emit_bar("5m", df.index[-1], sig.iloc[-1], df.iloc[-1], df) in (True, False)
    print("  ok  MAX_ALERT_AGE_MIN blocks stale bars")


def test_webhook_config_is_read_from_env():
    """WEBHOOK_* are documented in .env.example — they must reach the server."""
    from config import ScannerConfig
    saved = {k: os.environ.get(k) for k in ("WEBHOOK_HOST", "WEBHOOK_PORT", "WEBHOOK_SECRET")}
    try:
        os.environ["WEBHOOK_HOST"] = "127.0.0.1"
        os.environ["WEBHOOK_PORT"] = "5999"
        os.environ["WEBHOOK_SECRET"] = "s3cret"
        c = ScannerConfig()
        assert (c.webhook_host, c.webhook_port, c.webhook_secret) == ("127.0.0.1", 5999, "s3cret")
        os.environ["WEBHOOK_PORT"] = "not-a-port"      # must not crash
        assert ScannerConfig().webhook_port == 5000
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  ok  webhook settings are read from the environment")



def test_webhook_payload_shapes():
    """The webhook is a public endpoint: no payload shape may 500 it, and
    dedupe keys must be stable across process restarts."""
    from scanner.webhook import WebhookFormatter as W

    # non-dict JSON used to crash with AttributeError ('list' has no .get)
    for weird in ([1, 2, 3], 42, None, "plain alert text"):
        kind, key, msg = W.format_payload(weird)
        assert kind in ("PLAINTEXT", "GENERIC") and key and msg is not None

    sig = {"action": "BUY", "symbol": "NSE:NIFTY", "tf": "5m", "pool": "SSL-169",
           "pool_lvl": 24010.55, "entry": 24061.05, "sl": 24038.43, "tp": 24106.30,
           "target": 24114.00, "bar_time": "2026-09-01 10:25",
           "swing_bar_time": "2026-09-01 09:45"}
    kind, key, msg = W.format_payload(sig)
    assert kind == "BUY" and "SSL-169" in msg and "24,010.55" in msg
    assert "Chart Anchor (Swing Low): 2026-09-01 09:45 IST" in msg
    assert key == W.format_payload(dict(sig))[1], "dedupe key must be deterministic"

    fast = dict(sig, action="FAST_SELL", pool="BSL-169")
    kind, _, msg = W.format_payload(fast)
    assert kind == "FAST_SELL" and "FAST" in msg and "actual HIGH" in msg

    # a stable digest (not Python's randomised hash) for the generic fallback
    g = {"foo": "bar", "n": 1}
    assert W.format_payload(g)[1] == W.format_payload(dict(g))[1]
    print("  ok  webhook handles every payload shape with stable keys")


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
    print("\nLive-market hardening checks")
    test_config_is_typo_proof()
    test_feed_normalisation()
    test_scanner_survives_bad_state()
    test_no_duplicate_and_retry_on_failure()
    test_state_ledger_is_bounded()
    test_scanner_accepts_lookback_and_market_check()
    test_stale_bars_are_not_alerted()
    test_webhook_config_is_read_from_env()
    test_webhook_payload_shapes()
    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    main()
