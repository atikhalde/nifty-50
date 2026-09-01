"""Live scanner: yfinance feed -> BSL/SSL engine -> strict-dedupe -> dual Telegram.

This version is 100% parity with the Pine script `abcd.txt`:
  * ATR = Wilder RMA of TR (ta.atr)
  * Pivots = ta.pivothigh/low(_, pivLen, pivLen) -> signal fires pivLen bars after actual swing (non-repainting)
  * Pool lifecycle = create -> sweep -> touch -> expiry (same order as Pine)
  * Signal mapping = default fade BSL->SELL, SSL->BUY, magnet flips
  * SL/TP = close ∓ atr*atrSL / close ± atr*atrSL*rrTarget
  * Telegram message EXACTLY matches chart indicator's label values:
    - pool name, pool level (actual swing high/low)
    - entry = close of confirmation bar
    - SL/TP from ATR at confirmation bar
    - nearest pool = nextBSL/nextSSL (default) or pool itself (magnet) -> matches Pine's buyTgt/sellTgt
    - swing confirmation text with HIGH/LOW based on mapping
    - BOTH actual swing bar time AND confirmation bar time (Pine label anchored at actual, alert fires at confirmation)

If chart and scanner still differ, check:
  1. .env settings must match TradingView inputs (PIV_LEN, ZONE_ATR_MULT, EQ_TOL_ATR, MAX_POOLS, POOL_EXPIRY, ATR_SL, RR_TARGET, SIG_DIR)
  2. yfinance ^NSEI data can slightly differ from TradingView NSE:NIFTY feed -> ATR/SL/TP may differ by few paise
  3. yfinance 5m limited to 60 days, 1m to 7 days -> pool IDs diverge from long-history TradingView chart, but signal logic still converges after expiry window
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

from scanner.alerts.telegram import TelegramNotifier
from scanner.data.yfinance_feed import YFinanceFeed, INTERVAL_DELTA
from scanner.indicators.bsl_ssl import BSLSSLParams, compute_signals
from scanner.state import SentState

log = logging.getLogger("scanner")


def _fmt_inr(v: float) -> str:
    """Indian-style thousands grouping, e.g. 24,31,050.25"""
    if v is None or (isinstance(v, float) and (math.isnan(v))):
        return "—"
    neg = v < 0
    whole, _, dec = f"{abs(v):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return ("-" if neg else "") + whole + "." + dec


class LiveScanner:
    def __init__(self, cfg, params: BSLSSLParams | None = None, feed=None,
                 lookback_minutes: int = 0, market_check: bool = True):
        self.cfg = cfg
        self.params = params or BSLSSLParams.from_env()
        self.tz = ZoneInfo(cfg.tz)
        self.feed = feed or YFinanceFeed(
            symbol=cfg.symbol, tz=cfg.tz,
            session_start=cfg.session_start, session_end=cfg.session_end,
        )
        self.state = SentState(cfg.state_file)
        self.notifier = TelegramNotifier(
            bot1_token=cfg.bot1_token, bot2_token=cfg.bot2_token,
            chat_id=cfg.chat_id, chat_id_2=cfg.chat_id_2,
            enabled=cfg.telegram_enabled,
        )
        # Lookback mode (cloud/Actions): the engine runs on the FULL warm-up
        # window so its pool state converges exactly like a live scanner, but
        # alerts fire only for closed bars inside the last `lookback_minutes`.
        # Alert dedupe comes from the cached `data/sent_alerts.json` (shared
        # across fresh machines), NOT from last_evaluated.
        self.lookback_minutes = int(lookback_minutes or 0)
        self.market_check = market_check

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        log.info("BSL/SSL scanner started | symbol=%s | tf=%s | interval=%ss | pivLen=%s | sigDir=%s",
                 self.cfg.display_symbol, ",".join(self.cfg.timeframes),
                 self.cfg.scan_interval_sec, self.params.piv_len, self.params.sig_dir)
        if not self.notifier.enabled:
            log.info("Telegram is DISABLED — alerts will only be logged (dry-run).")
        try:
            while True:
                try:
                    self.tick()
                except Exception:
                    log.exception("scan cycle failed")
                time.sleep(self.cfg.scan_interval_sec)
        except KeyboardInterrupt:
            log.info("Shutting down…")
            self.state.persist()

    def tick(self) -> None:
        # In lookback mode the schedule already encodes market hours — never
        # gate on the local clock (an Actions run is a fresh machine, and the
        # cached window is what matters).
        if (self.market_check and self.cfg.market_hours_only
                and not self.lookback_minutes and not self._market_open()):
            log.debug("market closed — idle")
            return
        now = datetime.now(self.tz)
        for tf in self.cfg.timeframes:
            try:
                df = self.feed.get_bars(tf)
                if df is None or df.empty or len(df) < 2:
                    continue
                sig = compute_signals(df, self.params)
                self._process_new_closed_bars(tf, df, sig, now)
            except Exception:
                log.exception("timeframe %s failed", tf)

    # ------------------------------------------------------------------
    def _market_open(self) -> bool:
        now = datetime.now(self.tz)
        if now.weekday() >= 5:
            return False
        t = now.time()
        return dtime.fromisoformat(self.cfg.session_start) <= t <= dtime.fromisoformat(self.cfg.session_end)

    # ------------------------------------------------------------------
    def _process_new_closed_bars(self, tf, df, sig, now) -> None:
        delta = INTERVAL_DELTA.get(tf, pd.Timedelta(minutes=5))
        closed = df.index[(df.index + delta) <= now]
        if len(closed) == 0:
            return

        if self.lookback_minutes:
            # Cloud lookback: the engine already ran over the full warm-up
            # window (feed returns full history), so pool state converges like
            # a live scanner. Alert ONLY on closed bars inside the recent
            # lookback window; every alert key still goes through the
            # persistent dedupe, so replaying the window on every fresh run
            # can never send a duplicate.
            cutoff = df.index.max() - pd.Timedelta(minutes=self.lookback_minutes)
            new_bars = closed[closed > cutoff]
        else:
            last_ev = self.state.last_evaluated.get(tf)
            if last_ev is None:
                # First run for this timeframe: baseline only, do NOT alert history.
                self.state.set_last_evaluated(tf, closed[-1].isoformat())
                self.state.persist()
                log.info("[%s] baseline set at %s (skipping history)", tf, closed[-1])
                return

            threshold = pd.Timestamp(last_ev).tz_convert(self.tz)
            new_bars = closed[closed > threshold]
            if len(new_bars) == 0:
                return

        changed = False
        for ts in new_bars:
            row = sig.loc[ts]
            bar = df.loc[ts]
            # Actual swing bar = confirmation bar - piv_len (Pine: bar_index - pivLen)
            # This is where Pine draws the label, while alert fires at confirmation bar
            try:
                pos = df.index.get_loc(ts)
                actual_pos = pos - self.params.piv_len
                actual_ts = df.index[actual_pos] if actual_pos >= 0 else None
            except Exception:
                actual_ts = None
            changed |= self._emit_bar(tf, ts, row, bar, actual_ts, df)

        if self.lookback_minutes:
            if changed:
                self.state.persist()
                log.info("[%s] lookback: %d closed bar(s) in the last %dm window, alerts fired",
                         tf, len(new_bars), self.lookback_minutes)
            else:
                log.info("[%s] lookback: %d closed bar(s) in window, nothing new to send", tf, len(new_bars))
                # persist ONLY on change — a no-change run leaves the cached
                # state file untouched (no useless cache churn every 5 min)
                self.state.persist()
                log.info("[%s] lookback: %d closed bar(s) in the last %dm "
                         "window, alerts fired",
                         tf, len(new_bars), self.lookback_minutes)
            else:
                log.info("[%s] lookback: %d closed bar(s) in window, nothing "
                         "new to send", tf, len(new_bars))
        else:
            self.state.set_last_evaluated(tf, new_bars[-1].isoformat())
            self.state.persist()
            if changed:
                log.info("[%s] processed %d new closed bar(s), last=%s", tf, len(new_bars), new_bars[-1])
                log.info("[%s] processed %d new closed bar(s), last=%s",
                         tf, len(new_bars), new_bars[-1])

    # ------------------------------------------------------------------
    def _emit_bar(self, tf, ts, row, bar, actual_ts, df) -> bool:
        alerts = []
        if self.params.show_signals and bool(row["buy_sig"]):
            alerts.append(("BUY", self._build_signal_msg("BUY", tf, ts, row, bar, actual_ts)))
        if self.params.show_signals and bool(row["sell_sig"]):
            alerts.append(("SELL", self._build_signal_msg("SELL", tf, ts, row, bar, actual_ts)))
        if self.cfg.sweep_alerts:
            if bool(row["swept_ssl"]):
                alerts.append(("SWEEP_SSL", self._build_sweep_msg("SSL", tf, ts, row, bar)))
            if bool(row["swept_bsl"]):
                alerts.append(("SWEEP_BSL", self._build_sweep_msg("BSL", tf, ts, row, bar)))

        changed = False
        for kind, text in alerts:
            lvl = self._level_of(kind, row)
            # Unique key = symbol|tf|kind|bar-time|level -> exactly once forever
            key = f"{self.cfg.display_symbol}|{tf}|{kind}|{ts.isoformat()}|{round(lvl, 4) if lvl == lvl else 'na'}"
            if self.state.already_sent(key):
                continue
            result = self.notifier.send(text)
            if result is False:
                if self.lookback_minutes:
                    log.warning("Alert delivery failed in lookback run, will try next run: %s", key)
                    # one-shot run: log and move on — the same bar will be
                    # inside the next run's window, but the key was NOT marked,
                    # so it gets exactly one more chance next run
                    log.warning("Alert delivery failed in lookback run, will try "
                                "next run: %s", key)
                else:
                    log.warning("Alert delivery failed, will retry next cycle: %s", key)
            else:
                self.state.mark(key)
                changed = True
        return changed

    def _level_of(self, kind: str, row) -> float:
        # Which pool a BUY/SELL refers to depends on the signal mapping:
        #   default (fade)  BUY <- fresh SSL, SELL <- fresh BSL
        #   magnet          BUY <- fresh BSL, SELL <- fresh SSL
        magnet = self.params.magnet
        if kind == "BUY":
            return row["new_bsl_lvl"] if magnet else row["new_ssl_lvl"]
        if kind == "SELL":
            return row["new_ssl_lvl"] if magnet else row["new_bsl_lvl"]
        if kind == "SWEEP_SSL":
            return row["swept_ssl_lvl"]
        if kind == "SWEEP_BSL":
            return row["swept_bsl_lvl"]
        return float("nan")

    # ------------------------------------------------------------------
    # message builders - EXACTLY match Pine chart indicator
    # ------------------------------------------------------------------
    def _build_signal_msg(self, side: str, tf, ts, row, bar, actual_ts) -> str:
        """
        Build telegram message that EXACTLY matches Pine indicator's label + alert.

        Pine label (anchored at actual swing bar):
          BUY · SSL-05 start
          Entry 24280.00
          SL 24205.00
          TP 24430.00
          → pool 24300.00

        Pine alert:
          BUY [SSL-05 START] pool @ 24310.00 | Entry 24280.00 SL 24205.00 TP 24430.00 | swing confirmed 8 bars after the actual low

        Our telegram combines both and adds bar times for 100% traceability:
          - pool name + level (actual swing high/low)
          - entry = close of confirmation bar (Pine's close)
          - SL/TP = close ∓ atr*1.2 / close ± atr*1.2*2.0 (Pine's slLong/tpLong)
          - nearest pool = nextBSL/nextSSL (default) or pool itself (magnet) = Pine's buyTgt/sellTgt
          - swing confirmed X bars after actual HIGH/LOW (non-repainting) with actual bar time
          - confirmation bar time (when signal fires) + actual swing bar time (where label is drawn)
        """
        sym = self.cfg.display_symbol
        entry = float(bar["close"])
        atr = float(row["atr"]) if row["atr"] == row["atr"] else 0.0

        # Pool mapping mirrors the Pine script:
        #   default (fade):  buyPool = new SSL, sellPool = new BSL
        #                    buyTgt  = nextBSL, sellTgt  = nextSSL
        #   magnet:          buyPool = new BSL, sellPool = new SSL
        #                    buyTgt  = new BSL, sellTgt  = new SSL (the pool itself)
        magnet = self.params.magnet
        if side == "BUY":
            if magnet:
                pool_name, pool_lvl = row["new_bsl_name"], row["new_bsl_lvl"]
                target = row["new_bsl_lvl"]  # Pine: buyTgt = newBSLlvl in magnet mode
                swing_word = "HIGH"
                actual_side = "HIGH"
            else:
                pool_name, pool_lvl = row["new_ssl_name"], row["new_ssl_lvl"]
                target = row["next_bsl"]      # Pine: buyTgt = nextBSL in fade mode
                swing_word = "LOW"
                actual_side = "LOW"
            sl, tp = row["sl_long"], row["tp_long"]
            head = f"🟢 BUY SIGNAL — {sym} ({tf})"
        else:  # SELL
            if magnet:
                pool_name, pool_lvl = row["new_ssl_name"], row["new_ssl_lvl"]
                target = row["new_ssl_lvl"]  # Pine: sellTgt = newSSLlvl in magnet
                swing_word = "LOW"
                actual_side = "LOW"
            else:
                pool_name, pool_lvl = row["new_bsl_name"], row["new_bsl_lvl"]
                target = row["next_ssl"]      # Pine: sellTgt = nextSSL in fade
                swing_word = "HIGH"
                actual_side = "HIGH"
            sl, tp = row["sl_short"], row["tp_short"]
                target = row["new_bsl_lvl"]
                swing_word = "HIGH"
            else:
                pool_name, pool_lvl = row["new_ssl_name"], row["new_ssl_lvl"]
                target = row["next_bsl"]
                swing_word = "LOW"
            sl, tp = row["sl_long"], row["tp_long"]
            swing_txt = (f"Swing confirmed {self.params.piv_len} bars after the "
                         f"actual {swing_word} (non-repainting)")
            head = f"🟢 BUY SIGNAL — {sym} ({tf})"
        else:
            if magnet:
                pool_name, pool_lvl = row["new_ssl_name"], row["new_ssl_lvl"]
                target = row["new_ssl_lvl"]
                swing_word = "LOW"
            else:
                pool_name, pool_lvl = row["new_bsl_name"], row["new_bsl_lvl"]
                target = row["next_ssl"]
                swing_word = "HIGH"
            sl, tp = row["sl_short"], row["tp_short"]
            swing_txt = (f"Swing confirmed {self.params.piv_len} bars after the "
                         f"actual {swing_word} (non-repainting)")
            head = f"🔴 SELL SIGNAL — {sym} ({tf})"

        # Format times
        conf_time_str = ts.strftime('%Y-%m-%d %H:%M')
        actual_time_str = actual_ts.strftime('%Y-%m-%d %H:%M') if actual_ts is not None else "—"

        # Build message - matches Pine's alert + label + extra traceability
        lines = [
            head,
            f"📌 Fresh {pool_name} pool start @ {_fmt_inr(pool_lvl)}",
            f"Entry: {_fmt_inr(entry)}",
            f"SL: {_fmt_inr(sl)}  ·  TP: {_fmt_inr(tp)}",
        ]
        # Nearest pool - only show if valid (matches Pine's na check)
        if target is not None and target == target and not math.isnan(target):
            # For default mode, this is opposite side pool; for magnet, it's the pool itself (same as pool_lvl)
            # To avoid confusion when magnet shows same level twice, we label it clearly
            if magnet:
                # In magnet mode Pine's buyTgt/sellTgt IS the pool itself, so we show as target
                lines.append(f"🎯 Target pool: {_fmt_inr(target)}")
            else:
                lines.append(f"🎯 Nearest pool: {_fmt_inr(target)}")

        # Swing confirmation - EXACTLY matches Pine's wording
        lines.append(f"Swing confirmed {self.params.piv_len} bars after the actual {swing_word} (non-repainting)")

        # Bar times - both actual swing (where label is drawn) and confirmation (where alert fires)
        # This makes telegram 100% traceable to chart
        lines.append(f"Bar: {conf_time_str} IST")
        if actual_ts is not None:
            lines.append(f"Actual {actual_side}: {actual_time_str} IST")

        # Optional: add ATR for debugging exact match with chart (commented, but useful for parity check)
        # lines.append(f"ATR: {_fmt_inr(atr)}")

        if target is not None and target == target:   # NaN-safe check
            lines.append(f"🎯 Nearest pool: {_fmt_inr(target)}")
        lines += [
            swing_txt,
            f"Bar: {ts.strftime('%Y-%m-%d %H:%M')} IST",
        ]
        return "\n".join(lines)

    def _build_sweep_msg(self, side: str, tf, ts, row, bar) -> str:
        """
        Sweep message - matches Pine's sweep markers:
          SSL sweep = low < lvl and close > lvl -> bullish (price reclaimed)
          BSL sweep = high > lvl and close < lvl -> bearish (price failed)
        """
        sym = self.cfg.display_symbol
        close = float(bar["close"])
        if side == "SSL":
            lvl = row["swept_ssl_lvl"]
            tone = "bullish (price reclaimed the level)"
            head = f"🧹 SSL SWEPT — {sym} ({tf})"
            arrow = "📈"
            kind_txt = "Sell-side liquidity pool"
            # Extra detail: close vs level
            detail = f"Close {_fmt_inr(close)} > level {_fmt_inr(lvl)}" if close == close else ""
        else:
            lvl = row["swept_bsl_lvl"]
            tone = "bearish (price failed above the level)"
            head = f"🧹 BSL SWEPT — {sym} ({tf})"
            arrow = "📉"
            kind_txt = "Buy-side liquidity pool"
            detail = f"Close {_fmt_inr(close)} < level {_fmt_inr(lvl)}" if close == close else ""

        lines = [
            head,
            f"{arrow} {kind_txt} @ {_fmt_inr(lvl)} was swept — {tone}",
        ]
        if detail:
            lines.append(detail)
        lines.append(f"Bar: {ts.strftime('%Y-%m-%d %H:%M')} IST")
        return "\n".join(lines)
