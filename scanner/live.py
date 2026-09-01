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
    # Never fire more than this many historical bars in one cycle.
    MAX_BACKLOG_BARS = 5

    def __init__(self, cfg, params: BSLSSLParams | None = None, feed=None):
        self.cfg = cfg
        self.params = params or BSLSSLParams.from_env()
        self.tz = ZoneInfo(cfg.tz)
        # Parse the session window once; a bad value must not break every tick.
        self._session_start = self._parse_time(cfg.session_start, dtime(9, 15))
        self._session_end = self._parse_time(cfg.session_end, dtime(15, 30))
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

    @staticmethod
    def _parse_time(value: str, fallback: dtime) -> dtime:
        try:
            return dtime.fromisoformat(str(value).strip())
        except (ValueError, TypeError):
            log.warning("Invalid session time %r — using %s", value, fallback)
            return fallback

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        log.info("BSL/SSL scanner started | symbol=%s | tf=%s | interval=%ss",
                 self.cfg.display_symbol, ",".join(self.cfg.timeframes),
                 self.cfg.scan_interval_sec)
        if not self.notifier.enabled:
            log.info("Telegram is DISABLED — alerts will only be logged (dry-run).")
        for problem in getattr(self.cfg, "validate", lambda: [])():
            log.warning("config: %s", problem)

        consecutive_failures = 0
        try:
            while True:
                started = time.monotonic()
                try:
                    self.tick()
                    consecutive_failures = 0
                except Exception:
                    consecutive_failures += 1
                    log.exception("scan cycle failed (%d in a row)", consecutive_failures)
                    if consecutive_failures in (5, 25, 100):
                        log.error("scanner has failed %d consecutive cycles — "
                                  "check network/feed/credentials", consecutive_failures)
                # Drift-corrected sleep: a slow cycle must not push the next
                # scan past the close of the following bar.
                elapsed = time.monotonic() - started
                time.sleep(max(1.0, self.cfg.scan_interval_sec - elapsed))
        except KeyboardInterrupt:
            log.info("Shutting down…")
        finally:
            self.state.persist()

    def tick(self) -> None:
        if self.cfg.market_hours_only and not self._market_open():
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
        return self._session_start <= t <= self._session_end

    # ------------------------------------------------------------------
    def _to_local_ts(self, value):
        """Parse a stored/observed timestamp into a tz-aware local Timestamp.

        State written by an older build (or edited by hand) can be tz-naive;
        `tz_convert` would raise on it and kill the scan cycle.
        """
        ts = pd.Timestamp(value)
        if ts.tzinfo is None or ts.tz is None:
            return ts.tz_localize(self.tz)
        return ts.tz_convert(self.tz)

    # ------------------------------------------------------------------
    def _process_new_closed_bars(self, tf, df, sig, now) -> None:
        delta = INTERVAL_DELTA.get(tf, pd.Timedelta(minutes=5))
        closed = df.index[(df.index + delta) <= now]
        if len(closed) == 0:
            return

        last_ev = self.state.last_evaluated.get(tf)
        if last_ev is None:
            # First run for this timeframe: baseline only, do NOT alert history.
            self.state.set_last_evaluated(tf, closed[-1].isoformat())
            self.state.persist()
            log.info("[%s] baseline set at %s (skipping history)", tf, closed[-1])
            return

        try:
            threshold = self._to_local_ts(last_ev)
        except Exception:
            log.exception("[%s] unreadable last_evaluated %r — re-baselining", tf, last_ev)
            self.state.set_last_evaluated(tf, closed[-1].isoformat())
            self.state.persist()
            return

        new_bars = closed[closed > threshold]
        if len(new_bars) == 0:
            return

        # A feed hiccup (or a very long outage) can hand us a large backlog.
        # Alerting on stale bars is worse than skipping them in a live market.
        max_backlog = self.MAX_BACKLOG_BARS
        if len(new_bars) > max_backlog:
            log.warning("[%s] %d new closed bars (feed gap?) — only alerting the newest %d",
                        tf, len(new_bars), max_backlog)
            new_bars = new_bars[-max_backlog:]

        changed = False
        for ts in new_bars:
            try:
                row = sig.loc[ts]
                bar = df.loc[ts]
                # Defensive: a duplicated index would make .loc return a frame.
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[-1]
                if isinstance(bar, pd.DataFrame):
                    bar = bar.iloc[-1]
            except KeyError:
                log.warning("[%s] bar %s vanished from the frame — skipping", tf, ts)
                continue
            changed |= self._emit_bar(tf, ts, row, bar)

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
                log.warning("Alert delivery failed, will retry next cycle: %s", key)
            else:
                # delivered (True) or dry-run/logged (None) — never send again
                self.state.mark(key)
                changed = True
        return changed

    @staticmethod
    def _level_of(kind: str, row) -> float:
        if kind == "BUY":
            return row["new_ssl_lvl"]
        if kind == "SELL":
            return row["new_bsl_lvl"]
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
        if side == "BUY":
            pool_name, pool_lvl = row["new_ssl_name"], row["new_ssl_lvl"]
            sl, tp = row["sl_long"], row["tp_long"]
            target = row["next_bsl"]
            swing_txt = f"Swing confirmed {self.params.piv_len} bars after the actual LOW (non-repainting)"
            head = f"🟢 BUY SIGNAL — {sym} ({tf})"
        else:
            pool_name, pool_lvl = row["new_bsl_name"], row["new_bsl_lvl"]
            sl, tp = row["sl_short"], row["tp_short"]
            target = row["next_ssl"]
            swing_txt = f"Swing confirmed {self.params.piv_len} bars after the actual HIGH (non-repainting)"
            head = f"🔴 SELL SIGNAL — {sym} ({tf})"

        lines = [
            head,
            f"📌 Fresh {pool_name} pool start @ {_fmt_inr(pool_lvl)}",
            f"Entry: {_fmt_inr(entry)}",
            f"SL: {_fmt_inr(sl)}  ·  TP: {_fmt_inr(tp)}",
        ]
        if target == target and not math.isnan(target):   # NaN-safe check
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
