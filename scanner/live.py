"""Live scanner: yfinance feed -> BSL/SSL engine -> strict-dedupe -> dual Telegram."""

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
        log.info("BSL/SSL scanner started | symbol=%s | tf=%s | interval=%ss",
                 self.cfg.display_symbol, ",".join(self.cfg.timeframes),
                 self.cfg.scan_interval_sec)
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
        if now.weekday() >= 5:          # Sat/Sun
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
            changed |= self._emit_bar(tf, ts, row, bar)

        if self.lookback_minutes:
            if changed:
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
                log.info("[%s] processed %d new closed bar(s), last=%s",
                         tf, len(new_bars), new_bars[-1])

    # ------------------------------------------------------------------
    def _emit_bar(self, tf, ts, row, bar) -> bool:
        alerts = []
        if self.params.show_signals and bool(row["buy_sig"]):
            alerts.append(("BUY", self._build_signal_msg("BUY", tf, ts, row, bar)))
        if self.params.show_signals and bool(row["sell_sig"]):
            alerts.append(("SELL", self._build_signal_msg("SELL", tf, ts, row, bar)))
        if self.cfg.sweep_alerts:
            if bool(row["swept_ssl"]):
                alerts.append(("SWEEP_SSL", self._build_sweep_msg("SSL", tf, ts, row)))
            if bool(row["swept_bsl"]):
                alerts.append(("SWEEP_BSL", self._build_sweep_msg("BSL", tf, ts, row)))

        changed = False
        for kind, text in alerts:
            lvl = self._level_of(kind, row)
            key = f"{self.cfg.display_symbol}|{tf}|{kind}|{ts.isoformat()}|{round(lvl, 4) if lvl == lvl else 'na'}"
            if self.state.already_sent(key):
                continue
            result = self.notifier.send(text)
            if result is False:
                if self.lookback_minutes:
                    # one-shot run: log and move on — the same bar will be
                    # inside the next run's window, but the key was NOT marked,
                    # so it gets exactly one more chance next run
                    log.warning("Alert delivery failed in lookback run, will try "
                                "next run: %s", key)
                else:
                    log.warning("Alert delivery failed, will retry next cycle: %s", key)
            else:
                # delivered (True) or dry-run/logged (None) — never send again
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
    # message builders
    # ------------------------------------------------------------------
    def _build_signal_msg(self, side: str, tf, ts, row, bar) -> str:
        sym = self.cfg.display_symbol
        entry = float(bar["close"])
        # Pool mapping mirrors the Pine script:
        #   default (fade):  buyPool = new SSL, sellPool = new BSL
        #                    buyTgt  = nextBSL, sellTgt  = nextSSL
        #   magnet:          buyPool = new BSL, sellPool = new SSL
        #                    buyTgt  = new BSL, sellTgt  = new SSL (the pool itself)
        magnet = self.params.magnet
        if side == "BUY":
            if magnet:
                pool_name, pool_lvl = row["new_bsl_name"], row["new_bsl_lvl"]
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

        lines = [
            head,
            f"📌 Fresh {pool_name} pool start @ {_fmt_inr(pool_lvl)}",
            f"Entry: {_fmt_inr(entry)}",
            f"SL: {_fmt_inr(sl)}  ·  TP: {_fmt_inr(tp)}",
        ]
        if target is not None and target == target:   # NaN-safe check
            lines.append(f"🎯 Nearest pool: {_fmt_inr(target)}")
        lines += [
            swing_txt,
            f"Bar: {ts.strftime('%Y-%m-%d %H:%M')} IST",
        ]
        return "\n".join(lines)

    def _build_sweep_msg(self, side: str, tf, ts, row) -> str:
        sym = self.cfg.display_symbol
        if side == "SSL":
            lvl = row["swept_ssl_lvl"]
            tone = "bullish (price reclaimed the level)"
            head = f"🧹 SSL SWEPT — {sym} ({tf})"
            arrow = "📈"
        else:
            lvl = row["swept_bsl_lvl"]
            tone = "bearish (price failed above the level)"
            head = f"🧹 BSL SWEPT — {sym} ({tf})"
            arrow = "📉"
        kind_txt = "Sell-side liquidity pool" if side == "SSL" else "Buy-side liquidity pool"
        return "\n".join([
            head,
            f"{arrow} {kind_txt} @ {_fmt_inr(lvl)} was swept — {tone}",
            f"Bar: {ts.strftime('%Y-%m-%d %H:%M')} IST",
        ])
