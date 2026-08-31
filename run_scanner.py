#!/usr/bin/env python3
"""BSL/SSL Liquidity scanner for NSE:NIFTY (1m & 5m) with dual Telegram alerts.

Usage:
    python run_scanner.py                 # live scan (yfinance + Telegram)
    python run_scanner.py --mock          # offline preview on synthetic data
    python run_scanner.py --once          # single scan cycle, then exit
    python run_scanner.py --mock --once   # single offline cycle (no network)
    python run_scanner.py --lookback 20   # scan last 20m of closed bars, exit
                                          # (GitHub Actions mode; also set via
                                          #  LOOKBACK_MINUTES in the env)
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys

from config import ScannerConfig
from scanner.alerts.telegram import TelegramNotifier
from scanner.data.mock import MockFeed
from scanner.indicators.bsl_ssl import BSLSSLParams
from scanner.live import LiveScanner


def _setup_logging(cfg: ScannerConfig, verbose: bool) -> None:
    level = logging.DEBUG if verbose else getattr(logging, cfg.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    try:
        import os
        os.makedirs(os.path.dirname(cfg.log_file) or ".", exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(cfg.log_file, maxBytes=2_000_000, backupCount=3)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:  # logging must never crash the scanner
        print(f"warning: could not attach file logger ({e})", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="BSL/SSL liquidity scanner for NSE:NIFTY")
    ap.add_argument("--mock", action="store_true",
                    help="Run on synthetic data (no yfinance/network needed)")
    ap.add_argument("--once", action="store_true",
                    help="Run one scan cycle and exit")
    ap.add_argument("--lookback", type=int, default=None,
                    help="Scan the last N minutes of closed bars and exit "
                         "(defaults to LOOKBACK_MINUTES from the environment)")
    ap.add_argument("--dump-sample", action="store_true",
                    help="Print sample BUY/SELL/sweep Telegram messages (offline) and exit")
    ap.add_argument("--verbose", action="store_true", help="Debug logging")
    args = ap.parse_args()

    cfg = ScannerConfig()
    _setup_logging(cfg, args.verbose)

    params = BSLSSLParams.from_env()
    feed = MockFeed() if args.mock else None
    lookback = int(args.lookback if args.lookback is not None else cfg.lookback_minutes)

    if args.mock:
        cfg.market_hours_only = False   # synthetic data is always "open"
        if not args.once:
            cfg.scan_interval_sec = min(cfg.scan_interval_sec, 2)  # faster demo

    scanner = LiveScanner(cfg, params=params, feed=feed,
                          lookback_minutes=lookback,
                          market_check=not lookback)

    if args.dump_sample:
        from scanner.data.mock import make_mock_bars
        from scanner.indicators.bsl_ssl import compute_signals
        df = make_mock_bars()
        sig = compute_signals(df, params)
        rows = sig[(sig["buy_sig"]) | (sig["sell_sig"]) | (sig["swept_ssl"]) | (sig["swept_bsl"])]
        if rows.empty:
            print("No signals found in synthetic data (should not happen).")
            return 1
        print("=" * 60)
        print("SAMPLE TELEGRAM MESSAGES (offline, from synthetic data)")
        print("=" * 60)
        for ts, row in list(rows.iterrows())[-6:]:
            bar = df.loc[ts]
            msgs = []
            if bool(row["buy_sig"]):
                msgs.append(scanner._build_signal_msg("BUY", "5m", ts, row, bar))
            if bool(row["sell_sig"]):
                msgs.append(scanner._build_signal_msg("SELL", "5m", ts, row, bar))
            if bool(row["swept_ssl"]):
                msgs.append(scanner._build_sweep_msg("SSL", "5m", ts, row))
            if bool(row["swept_bsl"]):
                msgs.append(scanner._build_sweep_msg("BSL", "5m", ts, row))
            print("\n" + "-" * 60)
            for m in msgs:
                print(m)
        return 0

    if not scanner.notifier.bots and cfg.telegram_enabled:
        log = logging.getLogger("scanner")
        log.warning("No Telegram credentials found — set BOT1_TOKEN/BOT2_TOKEN and CHAT_ID in .env "
                    "(see .env.example). Alerts will only be logged until then.")

    if args.once or lookback:
        scanner.tick()
        scanner.state.persist()
        logging.getLogger("scanner").info(
            "single cycle done%s", " (lookback %dm)" % lookback if lookback else "")
        return 0

    scanner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
