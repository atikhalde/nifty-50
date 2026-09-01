"""Live scanner: yfinance feed -> BSL/SSL engine -> strict-dedupe -> dual Telegram.

This version is 100% parity with the Pine script `abcd.txt`, plus Multi-Speed tiers:
  * ATR = Wilder RMA of TR (ta.atr)
  * Pivots = ta.pivothigh/low(_, pivLen, pivLen) -> signal fires pivLen bars after actual swing (non-repainting)
  * Pool lifecycle = create -> sweep -> touch -> expiry (same order as Pine)
  * Signal mapping = default fade BSL->SELL, SSL->BUY, magnet flips
  * SL/TP = close ∓ atr*atrSL / close ± atr*atrSL*rrTarget
  * Multi-Speed:
    - TIER 1 INSTANT — 0-bar lag on sweep candle close, wick SL, 1:2 R:R TP
    - TIER 2 FAST    — fast_piv_len=3 (15m on 5m / 3m on 1m), 62% faster entries
    - TIER 3 STANDARD — original piv_len=8 intact for macro target tracking
  * Telegram message EXACTLY matches chart indicator's label values:
    - pool name, pool level (actual swing high/low)
    - entry = close of confirmation (or sweep) bar
    - SL/TP from ATR at confirmation bar (standard/fast) or wick (instant)
    - nearest pool = nextBSL/nextSSL (default) or pool itself (magnet) -> matches Pine's buyTgt/sellTgt
    - swing confirmation text with HIGH/LOW based on mapping
    - BOTH Chart Anchor time AND Execution Bar time for all 3 speed tiers

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
    # Never fire more than this many historical bars in one cycle.
    MAX_BACKLOG_BARS = 5

    def __init__(self, cfg, params: BSLSSLParams | None = None, feed=None,
                 lookback_minutes: int | None = None, market_check: bool = True):
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
        # Lookback mode (cloud/Actions): the engine runs on the FULL warm-up
        # window so its pool state converges exactly like a live scanner, but
        # alerts fire only for closed bars inside the last `lookback_minutes`.
        # Alert dedupe comes from the cached `data/sent_alerts.json` (shared
        # across fresh machines), NOT from last_evaluated.
        if lookback_minutes is None:
            lookback_minutes = getattr(cfg, "lookback_minutes", 0)
        self.lookback_minutes = int(lookback_minutes or 0)
        self.market_check = market_check
        # Consecutive per-timeframe cycle failures — escalates to CRITICAL so a
        # scanner that has stopped producing alerts is LOUD in the logs instead
        # of looking healthy (the old behaviour: exceptions swallowed quietly).
        self._tf_failures: dict[str, int] = {}

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
        log.info("BSL/SSL scanner started | symbol=%s | tf=%s | interval=%ss | pivLen=%s | fastPiv=%s | sigDir=%s",
                 self.cfg.display_symbol, ",".join(self.cfg.timeframes),
                 self.cfg.scan_interval_sec, self.params.piv_len,
                 self.params.fast_piv_len, self.params.sig_dir)
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
        # In lookback mode the schedule already encodes market hours — never
        # gate on the local clock (an Actions run is a fresh machine, and the
        # cached window is what matters).
        if (self.market_check and self.cfg.market_hours_only
                and not self.lookback_minutes and not self._market_open()):
            log.debug("market closed — idle")
            return
        now = datetime.now(self.tz)
        self._retry_pending(now)
        for tf in self.cfg.timeframes:
            try:
                df = self.feed.get_bars(tf)
                if df is None or df.empty or len(df) < 2:
                    continue
                sig = compute_signals(df, self.params)
                self._process_new_closed_bars(tf, df, sig, now)
                self._tf_failures[tf] = 0
            except Exception:
                fails = self._tf_failures.get(tf, 0) + 1
                self._tf_failures[tf] = fails
                log.exception("timeframe %s failed", tf)
                if fails >= 5:
                    log.critical("timeframe %s has failed %d scan cycles IN A ROW — "
                                 "no alerts are being produced for it; investigate NOW",
                                 tf, fails)

    # ------------------------------------------------------------------
    def _market_open(self) -> bool:
        now = datetime.now(self.tz)
        if now.weekday() >= 5:
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
    def _retry_pending(self, now) -> None:
        """Re-deliver alerts that previously failed (never silently dropped).

        A pending alert older than `pending_max_age_min` is discarded with a
        warning — in live markets a signal that is that stale is more
        dangerous than a missed one.
        """
        if not self.state.pending:
            return
        max_age = pd.Timedelta(minutes=getattr(self.cfg, "pending_max_age_min", 30))
        changed = False
        for key, item in list(self.state.pending.items()):
            try:
                queued = pd.Timestamp(item.get("queued", now.isoformat()))
                queued = queued.tz_localize(self.tz) if queued.tz is None else queued.tz_convert(self.tz)
            except Exception:
                queued = pd.Timestamp(now)
            if pd.Timestamp(now) - queued > max_age:
                log.warning("Dropping stale undelivered alert (>%s old): %s", max_age, key)
                self.state.drop_pending(key)
                changed = True
                continue
            result = self.notifier.send(item.get("text", ""))
            if result is False:
                log.warning("Retry failed, will try again next cycle: %s", key)
            else:
                self.state.mark(key)
                log.info("Pending alert delivered on retry: %s", key)
                changed = True
        if changed:
            self.state.persist()

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

            try:
                threshold = self._to_local_ts(last_ev)
            except Exception:
                log.exception("[%s] unreadable last_evaluated %r — re-baselining", tf, last_ev)
                self.state.set_last_evaluated(tf, closed[-1].isoformat())
                self.state.persist()
                return

            new_bars = closed[closed > threshold]

            # A feed hiccup (or a very long outage) can hand us a large backlog.
            # Alerting on stale bars is worse than skipping them in a live market.
            if len(new_bars) > self.MAX_BACKLOG_BARS:
                log.warning("[%s] %d new closed bars (feed gap?) — only alerting the newest %d",
                            tf, len(new_bars), self.MAX_BACKLOG_BARS)
                new_bars = new_bars[-self.MAX_BACKLOG_BARS:]

        if len(new_bars) == 0:
            return

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
            changed |= self._emit_bar(tf, ts, row, bar, df)

        if self.lookback_minutes:
            if changed:
                self.state.persist()
                log.info("[%s] lookback: %d closed bar(s) in the last %dm window, alerts fired",
                         tf, len(new_bars), self.lookback_minutes)
            else:
                log.info("[%s] lookback: %d closed bar(s) in window, nothing new to send",
                         tf, len(new_bars))
        else:
            self.state.set_last_evaluated(tf, new_bars[-1].isoformat())
            self.state.persist()
            if changed:
                log.info("[%s] processed %d new closed bar(s), last=%s",
                         tf, len(new_bars), new_bars[-1])

    # ------------------------------------------------------------------
    def _emit_bar(self, tf, ts, row, bar, df) -> bool:
        # Stale-signal guard: a restart with an old state file, or a long feed
        # outage, must never fire entries for bars that are long gone.
        max_age = getattr(self.cfg, "max_alert_age_min", 10)
        age_min = (pd.Timestamp.now(tz=self.tz) - pd.Timestamp(ts)).total_seconds() / 60.0
        if max_age and age_min > max_age:
            log.warning("[%s] skipping stale bar %s (%.0fm old > MAX_ALERT_AGE_MIN=%s)",
                        tf, ts, age_min, max_age)
            return False

        # Chart-anchor (actual swing bar) timestamps per speed tier — mirrors
        # where the Pine label is drawn (bar_index - pivLen / - fastPivLen);
        # TIER 1 instant trades anchor on the sweep bar itself.
        pos = df.index.get_loc(ts)
        anchor_std = (df.index[pos - self.params.piv_len]
                      if pos >= self.params.piv_len else None)
        anchor_fast = (df.index[pos - self.params.fast_piv_len]
                       if self.params.fast_piv_len > 0 and pos >= self.params.fast_piv_len
                       else None)

        alerts = []
        if self.params.show_signals:
            # TIER 3 STANDARD (piv_len confirmation)
            if bool(row["buy_sig"]):
                alerts.append(("BUY", self._build_signal_msg(
                    "BUY", tf, ts, row, bar, anchor_std, speed="standard")))
            if bool(row["sell_sig"]):
                alerts.append(("SELL", self._build_signal_msg(
                    "SELL", tf, ts, row, bar, anchor_std, speed="standard")))
            # TIER 2 FAST (fast_piv_len confirmation; engine flags already
            # include the params.fast_signals master switch)
            if bool(row["fast_buy_sig"]):
                alerts.append(("FAST_BUY", self._build_signal_msg(
                    "BUY", tf, ts, row, bar, anchor_fast, speed="fast")))
            if bool(row["fast_sell_sig"]):
                alerts.append(("FAST_SELL", self._build_signal_msg(
                    "SELL", tf, ts, row, bar, anchor_fast, speed="fast")))
        # TIER 1 INSTANT sweep trades (engine flags already include the
        # params.instant_sweep_trades master switch)
        if bool(row["inst_buy_sig"]):
            alerts.append(("INST_BUY", self._build_instant_msg(
                "BUY", tf, ts, row, bar, ts)))
        if bool(row["inst_sell_sig"]):
            alerts.append(("INST_SELL", self._build_instant_msg(
                "SELL", tf, ts, row, bar, ts)))
        if self.cfg.sweep_alerts:
            if bool(row["swept_ssl"]):
                alerts.append(("SWEEP_SSL", self._build_sweep_msg("SSL", tf, ts, row, bar)))
            if bool(row["swept_bsl"]):
                alerts.append(("SWEEP_BSL", self._build_sweep_msg("BSL", tf, ts, row, bar)))

        changed = False
        for kind, text in alerts:
            lvl = self._level_of(kind, row)
            # Unique key = symbol|tf|kind|bar-time|level -> exactly once forever
            key = f"{self.cfg.display_symbol}|{tf}|{kind}|{ts.isoformat()}|{round(lvl, 4) if lvl == lvl and not math.isnan(lvl) else 'na'}"
            if self.state.already_sent(key):
                continue
            result = self.notifier.send(text)
            if result is False:
                # Queue for redelivery instead of losing the signal; the key is
                # only marked as sent once it has actually gone out.
                self.state.add_pending(
                    key, text,
                    pd.Timestamp.now(tz=self.tz).isoformat())
                changed = True
                if self.lookback_minutes:
                    log.warning("Alert delivery failed in lookback run, queued for retry: %s", key)
                else:
                    log.warning("Alert delivery failed, queued for retry: %s", key)
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
        if kind == "FAST_BUY":
            return row["fast_new_bsl_lvl"] if magnet else row["fast_new_ssl_lvl"]
        if kind == "FAST_SELL":
            return row["fast_new_ssl_lvl"] if magnet else row["fast_new_bsl_lvl"]
        if kind in ("INST_BUY", "SWEEP_SSL"):
            return row["swept_ssl_lvl"]
        if kind in ("INST_SELL", "SWEEP_BSL"):
            return row["swept_bsl_lvl"]
        return float("nan")

    # ------------------------------------------------------------------
    # message builders - EXACTLY match Pine chart indicator
    # ------------------------------------------------------------------
    def _build_signal_msg(self, side: str, tf, ts, row, bar, actual_ts, speed: str = "standard") -> str:
        """
        Build telegram message that EXACTLY matches Pine indicator's label + alert.

        Dual timestamps on every speed tier:
          Chart Anchor  = where the Pine label is drawn (actual swing bar)
          Execution Bar = when the alert fires (confirmation bar)

        speed="standard" (TIER 3): piv_len confirmation, ATR SL/TP, macro next-pool target
        speed="fast"     (TIER 2): fast_piv_len confirmation (62% faster), ATR SL/TP,
                                   still aims at the standard (piv_len=8) macro pool
        """
        sym = self.cfg.display_symbol
        entry = float(bar["close"])
        is_fast = speed == "fast"
        lag = self.params.fast_piv_len if is_fast else self.params.piv_len
        tag = "FAST" if is_fast else "STANDARD"

        magnet = self.params.magnet
        if side == "BUY":
            if is_fast:
                if magnet:
                    pool_name, pool_lvl = row["fast_new_bsl_name"], row["fast_new_bsl_lvl"]
                    # Prefer the intact standard-book target when present
                    target = row["next_bsl"] if not (isinstance(row["next_bsl"], float) and math.isnan(row["next_bsl"])) else row["fast_new_bsl_lvl"]
                    swing_word = "HIGH"
                    actual_side = "High"
                else:
                    pool_name, pool_lvl = row["fast_new_ssl_name"], row["fast_new_ssl_lvl"]
                    target = row["next_bsl"]
                    swing_word = "LOW"
                    actual_side = "Low"
                sl, tp = row["fast_sl_long"], row["fast_tp_long"]
            else:
                if magnet:
                    pool_name, pool_lvl = row["new_bsl_name"], row["new_bsl_lvl"]
                    target = row["new_bsl_lvl"]
                    swing_word = "HIGH"
                    actual_side = "High"
                else:
                    pool_name, pool_lvl = row["new_ssl_name"], row["new_ssl_lvl"]
                    target = row["next_bsl"]
                    swing_word = "LOW"
                    actual_side = "Low"
                sl, tp = row["sl_long"], row["tp_long"]
            head = f"🟢 BUY SIGNAL — {sym} ({tf}) · {tag}"
        else:  # SELL
            if is_fast:
                if magnet:
                    pool_name, pool_lvl = row["fast_new_ssl_name"], row["fast_new_ssl_lvl"]
                    target = row["next_ssl"]
                    swing_word = "LOW"
                    actual_side = "Low"
                else:
                    pool_name, pool_lvl = row["fast_new_bsl_name"], row["fast_new_bsl_lvl"]
                    target = row["next_ssl"]
                    swing_word = "HIGH"
                    actual_side = "High"
                sl, tp = row["fast_sl_short"], row["fast_tp_short"]
            else:
                if magnet:
                    pool_name, pool_lvl = row["new_ssl_name"], row["new_ssl_lvl"]
                    target = row["new_ssl_lvl"]
                    swing_word = "LOW"
                    actual_side = "Low"
                else:
                    pool_name, pool_lvl = row["new_bsl_name"], row["new_bsl_lvl"]
                    target = row["next_ssl"]
                    swing_word = "HIGH"
                    actual_side = "High"
                sl, tp = row["sl_short"], row["tp_short"]
            head = f"🔴 SELL SIGNAL — {sym} ({tf}) · {tag}"

        conf_time_str = ts.strftime("%Y-%m-%d %H:%M")
        actual_time_str = actual_ts.strftime("%Y-%m-%d %H:%M") if actual_ts is not None else None

        entry_note = "Closed Fast Confirmation Bar" if is_fast else "Closed Confirmation Bar"
        lines = [
            head,
            f"📌 Fresh {pool_name} pool start @ {_fmt_inr(pool_lvl)}",
            f"💵 Entry: {_fmt_inr(entry)} ({entry_note})",
            f"🛑 SL: {_fmt_inr(sl)}  ·  🎯 TP: {_fmt_inr(tp)} (1:2 R:R)",
        ]
        if target is not None and target == target and not math.isnan(float(target) if target is not None else float("nan")):
            if magnet and not is_fast:
                lines.append(f"🎯 Target pool: {_fmt_inr(target)}")
            else:
                lines.append(f"🎯 Nearest pool: {_fmt_inr(target)}")

        if actual_time_str:
            lines.append(f"📍 Chart Anchor (Swing {actual_side}): {actual_time_str} IST")

        if is_fast:
            pct = 0
            if self.params.piv_len:
                pct = round(100.0 * (self.params.piv_len - lag) / self.params.piv_len)
            lines.append(
                f"⚡ Fast swing confirmed {lag} bars after actual {swing_word} "
                f"({pct}% faster, non-repainting)"
            )
        else:
            lines.append(
                f"⚡ Swing confirmed {lag} bars after actual {swing_word} (non-repainting)"
            )
        lines.append(f"🕒 Execution Bar: {conf_time_str} IST")
        # Keep a plain "Bar:" alias so older parsers / tests still match
        lines.append(f"Bar: {conf_time_str} IST")

        return "\n".join(lines)

    def _build_instant_msg(self, side: str, tf, ts, row, bar, actual_ts) -> str:
        """TIER 1 instant sweep trade: 0-bar lag, tight wick SL, 1:2 R:R TP."""
        sym = self.cfg.display_symbol
        entry = float(bar["close"])
        tstr = ts.strftime("%Y-%m-%d %H:%M")
        anchor = actual_ts.strftime("%Y-%m-%d %H:%M") if actual_ts is not None else tstr

        def _name(val, fallback):
            return val if isinstance(val, str) and val else fallback

        if side == "BUY":
            pool_name = _name(row["swept_ssl_name"] if "swept_ssl_name" in row.index else "", "SSL")
            pool_lvl = row["swept_ssl_lvl"]
            sl, tp = row["inst_sl_long"], row["inst_tp_long"]
            head = f"⚡ INSTANT SWEEP BUY — {sym} ({tf}) · TIER 1"
            tone = "SSL sweep reclaim (bullish)"
            actual_side = "Sweep Low"
        else:
            pool_name = _name(row["swept_bsl_name"] if "swept_bsl_name" in row.index else "", "BSL")
            pool_lvl = row["swept_bsl_lvl"]
            sl, tp = row["inst_sl_short"], row["inst_tp_short"]
            head = f"⚡ INSTANT SWEEP SELL — {sym} ({tf}) · TIER 1"
            tone = "BSL sweep rejection (bearish)"
            actual_side = "Sweep High"

        lines = [
            head,
            f"📌 {tone} · {pool_name} @ {_fmt_inr(pool_lvl)}",
            f"💵 Entry: {_fmt_inr(entry)} (Sweep Candle Close)",
            f"🛑 SL: {_fmt_inr(sl)} (wick)  ·  🎯 TP: {_fmt_inr(tp)} (1:2 R:R)",
            f"📍 Chart Anchor ({actual_side}): {anchor} IST",
            "⚡ 0-bar lag — executed on sweep candle close",
            f"🕒 Execution Bar: {tstr} IST",
            f"Bar: {tstr} IST",
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
            head = f"🧹 SSL SWEPT (Bullish Reclaim) — {sym} ({tf})"
            arrow = "📈"
            kind_txt = "Sell-side liquidity pool"
            tone = "bullish (price reclaimed)"
            detail = f"Close {_fmt_inr(close)} > level {_fmt_inr(lvl)}" if close == close else ""
            marker = "📍 Chart Marker: ▲ Green Triangle below bar"
        else:
            lvl = row["swept_bsl_lvl"]
            head = f"🧹 BSL SWEPT (Bearish Rejection) — {sym} ({tf})"
            arrow = "📉"
            kind_txt = "Buy-side liquidity pool"
            tone = "bearish (price failed above)"
            detail = f"Close {_fmt_inr(close)} < level {_fmt_inr(lvl)}" if close == close else ""
            marker = "📍 Chart Marker: ▼ Red Triangle above bar"

        lines = [
            head,
            f"{arrow} {kind_txt} @ {_fmt_inr(lvl)} was swept — {tone}",
        ]
        if detail:
            lines.append(detail)
        lines.append(marker)
        lines.append(f"Bar: {ts.strftime('%Y-%m-%d %H:%M')} IST")
        return "\n".join(lines)
