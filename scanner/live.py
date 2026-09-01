"""Live scanner: yfinance feed -> BSL/SSL engine -> strict-dedupe -> dual Telegram."""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, time as dtime, timedelta
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
    def __init__(self, cfg, params: BSLSSLParams | None = None, feed=None):
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
    def _market_open(self, now: datetime | None = None) -> bool:
        """True during the NSE session, plus a short grace window after the
        close so the final bars (which close exactly at session_end, e.g. the
        15:25-15:30 5m bar) still get processed TODAY instead of slipping to
        the next morning. After the session no new bars can form, so the grace
        window can only flush late-fetched closed bars — it cannot invent
        extra signals.
        """
        now = now or datetime.now(self.tz)
        if now.weekday() >= 5:          # Sat/Sun
            return False
        start = dtime.fromisoformat(self.cfg.session_start)
        end_dt = datetime.combine(now.date(), dtime.fromisoformat(self.cfg.session_end),
                                  tzinfo=self.tz) + timedelta(minutes=getattr(self.cfg, "eod_grace_min", 0))
        return start <= now.time() <= end_dt.time()

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

        threshold = pd.Timestamp(last_ev).tz_convert(self.tz)
        new_bars = closed[closed > threshold]
        if len(new_bars) == 0:
            return

        changed = False
        for ts in new_bars:
            row = sig.loc[ts]
            bar = df.loc[ts]
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
        bots = self.notifier.bot_names
        for kind, text in alerts:
            lvl = self._level_of(kind, row, self.params.magnet)
            key = f"{self.cfg.display_symbol}|{tf}|{kind}|{ts.isoformat()}|{round(lvl, 4) if lvl == lvl else 'na'}"
            if self.state.already_sent(key):
                continue
            # per-bot delivery tracking: retry ONLY the bots that have not
            # acknowledged yet (no duplicates on the bot that succeeded)
            pending = [b for b in bots if not self.state.already_sent(f"{key}@{b}")]
            if bots and not pending:
                self.state.mark(key)     # upgraded legacy/partial state
                continue
            result = self.notifier.send(text, only=pending or None)
            if result is None:
                # dry-run / not configured: logged once, never sent again
                self.state.mark(key)
                changed = True
                continue
            for name, ok in result.items():
                if ok:
                    self.state.mark(f"{key}@{name}")
            if all(self.state.already_sent(f"{key}@{b}") for b in bots) or not bots:
                self.state.mark(key)
                changed = True
            else:
                log.warning("Partial delivery (%s), will retry missing bots next cycle: %s",
                            {k: v for k, v in result.items() if not v}, key)
        return changed

    @staticmethod
    def _level_of(kind: str, row, magnet: bool = False) -> float:
        """Pool level behind a BUY/SELL alert — direction mapping must mirror
        the Pine script: default (fade) BUY <= new SSL pool, SELL <= new BSL
        pool; magnet mode flips both."""
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
        magnet = self.params.magnet
        # Pine: default (fade)  BUY=SSL start / SELL=BSL start, target = nearest opposite pool
        #       magnet          BUY=BSL start / SELL=SSL start, target = the fresh pool itself
        if side == "BUY":
            pool_name = row["new_bsl_name"] if magnet else row["new_ssl_name"]
            pool_lvl = row["new_bsl_lvl"] if magnet else row["new_ssl_lvl"]
            sl, tp = row["sl_long"], row["tp_long"]
            target = pool_lvl if magnet else row["next_bsl"]
            swing_kind = "HIGH" if magnet else "LOW"
            swing_txt = f"Swing confirmed {self.params.piv_len} bars after the actual {swing_kind} (non-repainting)"
            head = f"🟢 BUY SIGNAL — {sym} ({tf})"
        else:
            pool_name = row["new_ssl_name"] if magnet else row["new_bsl_name"]
            pool_lvl = row["new_ssl_lvl"] if magnet else row["new_bsl_lvl"]
            sl, tp = row["sl_short"], row["tp_short"]
            target = pool_lvl if magnet else row["next_ssl"]
            swing_kind = "LOW" if magnet else "HIGH"
            swing_txt = f"Swing confirmed {self.params.piv_len} bars after the actual {swing_kind} (non-repainting)"
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
