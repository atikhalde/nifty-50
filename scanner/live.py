"""Live scanner: data feed -> BSL/SSL engine -> strict-dedupe -> dual Telegram."""

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


def _fmt_inr(v: float | None) -> str:
    """Indian-style thousands grouping, e.g. 24,31,050.25"""
    if v is None or (isinstance(v, float) and (math.isnan(v))):
        return "—"
    try:
        val = float(v)
    except (ValueError, TypeError):
        return str(v)
    neg = val < 0
    whole, _, dec = f"{abs(val):.2f}".partition(".")
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
            changed |= self._emit_bar(tf, ts, row, bar, df)

        self.state.set_last_evaluated(tf, new_bars[-1].isoformat())
        self.state.persist()
        if changed:
            log.info("[%s] processed %d new closed bar(s), last=%s",
                     tf, len(new_bars), new_bars[-1])

    # ------------------------------------------------------------------
    def _emit_bar(self, tf, ts, row, bar, df: pd.DataFrame | None = None) -> bool:
        alerts = []
        if self.params.show_signals and bool(row["buy_sig"]):
            alerts.append(("BUY", self._build_signal_msg("BUY", tf, ts, row, bar, df)))
        if self.params.show_signals and bool(row["sell_sig"]):
            alerts.append(("SELL", self._build_signal_msg("SELL", tf, ts, row, bar, df)))
        if self.cfg.sweep_alerts:
            if bool(row["swept_ssl"]):
                alerts.append(("SWEEP_SSL", self._build_sweep_msg("SSL", tf, ts, row, bar)))
            if bool(row["swept_bsl"]):
                alerts.append(("SWEEP_BSL", self._build_sweep_msg("BSL", tf, ts, row, bar)))

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
    def _build_signal_msg(self, side: str, tf, ts, row, bar, df: pd.DataFrame | None = None) -> str:
        sym = self.cfg.display_symbol
        entry = float(bar["close"])

        # Determine swing anchor bar timestamp
        piv_len = self.params.piv_len
        swing_ts_str = ""
        if df is not None and ts in df.index:
            loc = df.index.get_loc(ts)
            if isinstance(loc, int) and loc >= piv_len:
                swing_ts = df.index[loc - piv_len]
                swing_ts_str = swing_ts.strftime("%Y-%m-%d %H:%M")
        if not swing_ts_str:
            delta = INTERVAL_DELTA.get(tf, pd.Timedelta(minutes=5))
            swing_ts = ts - (piv_len * delta)
            swing_ts_str = swing_ts.strftime("%Y-%m-%d %H:%M")

        if side == "BUY":
            pool_name = row["new_ssl_name"] if not self.params.magnet else row["new_bsl_name"]
            pool_lvl = row["new_ssl_lvl"] if not self.params.magnet else row["new_bsl_lvl"]
            sl, tp = row["sl_long"], row["tp_long"]
            target = row["next_bsl"]
            head = f"🟢 BUY SIGNAL — {sym} ({tf})"
            anchor_txt = f"📍 Chart Anchor (Swing LOW): {swing_ts_str} IST"
            swing_txt = f"⚡ Swing confirmed {piv_len} bars after actual LOW (non-repainting)"
        else:
            pool_name = row["new_bsl_name"] if not self.params.magnet else row["new_ssl_name"]
            pool_lvl = row["new_bsl_lvl"] if not self.params.magnet else row["new_ssl_lvl"]
            sl, tp = row["sl_short"], row["tp_short"]
            target = row["next_ssl"]
            head = f"🔴 SELL SIGNAL — {sym} ({tf})"
            anchor_txt = f"📍 Chart Anchor (Swing HIGH): {swing_ts_str} IST"
            swing_txt = f"⚡ Swing confirmed {piv_len} bars after actual HIGH (non-repainting)"

        lines = [
            head,
            f"📌 Fresh {pool_name} pool start @ {_fmt_inr(pool_lvl)}",
            f"💵 Entry: {_fmt_inr(entry)} (Closed Confirmation Bar)",
            f"🛑 SL: {_fmt_inr(sl)}  ·  🎯 TP: {_fmt_inr(tp)} (1:2 R:R)",
        ]
        if target == target and not math.isnan(target):
            lines.append(f"🎯 Nearest Target Pool: {_fmt_inr(target)}")
        lines += [
            anchor_txt,
            swing_txt,
            f"Bar: {ts.strftime('%Y-%m-%d %H:%M')} IST",
        ]
        return "\n".join(lines)

    def _build_sweep_msg(self, side: str, tf, ts, row, bar) -> str:
        sym = self.cfg.display_symbol
        close_px = float(bar["close"])
        if side == "SSL":
            lvl = row["swept_ssl_lvl"]
            head = f"🧹 SSL SWEPT (Bullish Reclaim) — {sym} ({tf})"
            arrow = "📈"
            kind_txt = "Sell-side liquidity pool"
            action = "bullish (price reclaimed the level)"
            marker_txt = "📍 Chart Marker: ▲ Green Triangle below bar"
            detail_txt = f"Close {_fmt_inr(close_px)} > level {_fmt_inr(lvl)}"
        else:
            lvl = row["swept_bsl_lvl"]
            head = f"🧹 BSL SWEPT (Bearish Rejection) — {sym} ({tf})"
            arrow = "📉"
            kind_txt = "Buy-side liquidity pool"
            action = "bearish (price failed above the level)"
            marker_txt = "📍 Chart Marker: ▼ Red Triangle above bar"
            detail_txt = f"Close {_fmt_inr(close_px)} < level {_fmt_inr(lvl)}"

        return "\n".join([
            head,
            f"{arrow} {kind_txt} @ {_fmt_inr(lvl)} was swept — {action}",
            detail_txt,
            marker_txt,
            f"Bar: {ts.strftime('%Y-%m-%d %H:%M')} IST",
        ])
