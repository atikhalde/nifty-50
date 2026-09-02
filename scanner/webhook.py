"""TradingView Webhook receiver for instant (zero-delay) dual Telegram alerts."""

from __future__ import annotations

import hashlib
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
import math
import socketserver
from typing import Any

from scanner.alerts.telegram import TelegramNotifier
from scanner.parity import event_correlation, normalize_symbol, source_key
from scanner.state import SentState

log = logging.getLogger("webhook")

# Maximum accepted webhook body size (TradingView alert payloads are tiny).
MAX_BODY_BYTES = 64 * 1024


def _fmt_inr(v: float | None) -> str:
    """Format float into Indian Rupee numbering format."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
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


class WebhookFormatter:
    """Parses JSON or Plaintext TradingView alerts into dual-bot Telegram messages."""

    @staticmethod
    def format_payload(payload: dict[str, Any] | str, default_sym: str = "NSE:NIFTY",
                       source: str = "TRADINGVIEW") -> tuple[str, str, str]:
        """Returns (kind, source-specific dedupe key, formatted message)."""
        source = str(source).upper()
        if isinstance(payload, str):
            payload = payload.strip()
            if payload.startswith("{") and payload.endswith("}"):
                try:
                    payload = json.loads(payload)
                except Exception:
                    return WebhookFormatter._format_plaintext(payload, default_sym, source)
            else:
                return WebhookFormatter._format_plaintext(payload, default_sym, source)

        if not isinstance(payload, dict):
            return WebhookFormatter._format_plaintext(json.dumps(payload, default=str), default_sym, source)

        action = str(payload.get("action", "")).upper()
        speed = str(payload.get("speed", "")).strip().lower()
        if not speed:
            if "INST" in action:
                speed = "instant"
            elif "FAST" in action:
                speed = "fast"
            else:
                speed = "standard"
        try:
            piv_len = int(payload.get("piv_len", 3 if speed == "fast" else (0 if speed == "instant" else 4)))
        except (TypeError, ValueError):
            piv_len = 4 if speed == "standard" else (3 if speed == "fast" else 0)

        sym = normalize_symbol(payload.get("symbol", default_sym), default_sym)
        tf = payload.get("tf", "5m")
        bar_time = payload.get("bar_time", "")
        swing_time = payload.get("swing_bar_time", "")

        def _bar_line(t: str) -> str:
            if not t:
                return "🕒 Execution Bar: —"
            if str(t).endswith("IST"):
                return f"🕒 Execution Bar: {t}"
            return f"🕒 Execution Bar: {t} IST"

        def _plain_bar(t: str) -> str:
            if not t:
                return "Bar: —"
            return f"Bar: {t}" if str(t).endswith("IST") else f"Bar: {t} IST"

        def _pool_side(pool, fallback: str) -> str:
            explicit = str(payload.get("pool_side", "")).upper()
            if explicit in ("BSL", "SSL"):
                return explicit
            name = str(pool).upper()
            if "BSL" in name:
                return "BSL"
            if "SSL" in name:
                return "SSL"
            return fallback

        is_inst_buy = (
            "INST_BUY" in action or action in ("BUY_INSTANT", "INSTANT_BUY")
            or (speed == "instant" and "BUY" in action and "SWEEP_BSL" not in action)
        )
        is_inst_sell = (
            "INST_SELL" in action or action in ("SELL_INSTANT", "INSTANT_SELL")
            or (speed == "instant" and "SELL" in action and "SWEEP_SSL" not in action)
        )

        # --- TIER 1 instant sweep trades (check before generic BUY/SELL) ---
        if is_inst_buy and "SWEEP_SSL" not in action and "SSL_SWEPT" not in action:
            pool = payload.get("pool", "SSL")
            pool_lvl = float(payload.get("pool_lvl", 0.0) or 0.0)
            entry = float(payload.get("entry", payload.get("close", 0.0)) or 0.0)
            sl = float(payload.get("sl", 0.0) or 0.0)
            tp = float(payload.get("tp", 0.0) or 0.0)
            key = source_key(source, sym, tf, "INST_BUY", bar_time, pool_lvl)
            anchor = swing_time or bar_time
            lines = [
                f"⚡ INSTANT SWEEP BUY — {sym} ({tf}) · TIER 1",
                f"📌 SSL sweep reclaim (bullish) · {pool} @ {_fmt_inr(pool_lvl)}",
                f"💵 Entry: {_fmt_inr(entry)} (Sweep Candle Close)",
                f"🛑 SL: {_fmt_inr(sl)} (wick)  ·  🎯 TP: {_fmt_inr(tp)} (1:2 R:R)",
            ]
            if anchor:
                lines.append(f"📍 Chart Anchor (Sweep Low): {anchor} IST" if not str(anchor).endswith("IST") else f"📍 Chart Anchor (Sweep Low): {anchor}")
            lines += [
                "⚡ 0-bar lag — executed on sweep candle close",
                _bar_line(bar_time),
                _plain_bar(bar_time),
            ]
            return "INST_BUY", key, "\n".join(lines)

        if is_inst_sell and "SWEEP_BSL" not in action and "BSL_SWEPT" not in action:
            pool = payload.get("pool", "BSL")
            pool_lvl = float(payload.get("pool_lvl", 0.0) or 0.0)
            entry = float(payload.get("entry", payload.get("close", 0.0)) or 0.0)
            sl = float(payload.get("sl", 0.0) or 0.0)
            tp = float(payload.get("tp", 0.0) or 0.0)
            key = source_key(source, sym, tf, "INST_SELL", bar_time, pool_lvl)
            anchor = swing_time or bar_time
            lines = [
                f"⚡ INSTANT SWEEP SELL — {sym} ({tf}) · TIER 1",
                f"📌 BSL sweep rejection (bearish) · {pool} @ {_fmt_inr(pool_lvl)}",
                f"💵 Entry: {_fmt_inr(entry)} (Sweep Candle Close)",
                f"🛑 SL: {_fmt_inr(sl)} (wick)  ·  🎯 TP: {_fmt_inr(tp)} (1:2 R:R)",
            ]
            if anchor:
                lines.append(f"📍 Chart Anchor (Sweep High): {anchor} IST" if not str(anchor).endswith("IST") else f"📍 Chart Anchor (Sweep High): {anchor}")
            lines += [
                "⚡ 0-bar lag — executed on sweep candle close",
                _bar_line(bar_time),
                _plain_bar(bar_time),
            ]
            return "INST_SELL", key, "\n".join(lines)

        if "BUY" in action and "SWEEP" not in action:
            pool = payload.get("pool", "SSL")
            pool_side = _pool_side(pool, "SSL")
            anchor_word = "HIGH" if pool_side == "BSL" else "LOW"
            pool_lvl = float(payload.get("pool_lvl", 0.0))
            entry = float(payload.get("entry", 0.0))
            sl = float(payload.get("sl", 0.0))
            tp = float(payload.get("tp", 0.0))
            target = payload.get("target")
            target_str = _fmt_inr(float(target)) if target is not None and target != "" else None
            kind = "FAST_BUY" if speed == "fast" else "BUY"
            tag = "FAST" if speed == "fast" else "STANDARD"
            key = source_key(source, sym, tf, kind, bar_time, pool_lvl)
            entry_note = "Closed Fast Confirmation Bar" if speed == "fast" else "Closed Confirmation Bar"
            lines = [
                f"🟢 BUY SIGNAL — {sym} ({tf}) · {tag}",
                f"📌 Fresh {pool} pool start @ {_fmt_inr(pool_lvl)}",
                f"💵 Entry: {_fmt_inr(entry)} ({entry_note})",
                f"🛑 SL: {_fmt_inr(sl)}  ·  🎯 TP: {_fmt_inr(tp)} (1:2 R:R)",
            ]
            if target_str and target_str != "—":
                target_label = "Target pool" if abs(float(target) - pool_lvl) <= 0.011 else "Nearest pool"
                lines.append(f"🎯 {target_label}: {target_str}")
            if swing_time:
                st = swing_time if str(swing_time).endswith("IST") else f"{swing_time} IST"
                lines.append(f"📍 Chart Anchor (Swing {anchor_word.title()}): {st}")
            if speed == "fast":
                pct = round(100.0 * (4 - piv_len) / 4) if piv_len < 4 else 25
                lines.append(
                    f"⚡ Fast swing confirmed {piv_len} bars after actual {anchor_word} "
                    f"({pct}% faster, non-repainting)"
                )
            else:
                lines.append(
                    f"⚡ Swing confirmed {piv_len} bars after actual {anchor_word} (non-repainting)"
                )
            lines += [_bar_line(bar_time), _plain_bar(bar_time)]
            return kind, key, "\n".join(lines)

        elif "SELL" in action and "SWEEP" not in action:
            pool = payload.get("pool", "BSL")
            pool_side = _pool_side(pool, "BSL")
            anchor_word = "HIGH" if pool_side == "BSL" else "LOW"
            pool_lvl = float(payload.get("pool_lvl", 0.0))
            entry = float(payload.get("entry", 0.0))
            sl = float(payload.get("sl", 0.0))
            tp = float(payload.get("tp", 0.0))
            target = payload.get("target")
            target_str = _fmt_inr(float(target)) if target is not None and target != "" else None
            kind = "FAST_SELL" if speed == "fast" else "SELL"
            tag = "FAST" if speed == "fast" else "STANDARD"
            key = source_key(source, sym, tf, kind, bar_time, pool_lvl)
            entry_note = "Closed Fast Confirmation Bar" if speed == "fast" else "Closed Confirmation Bar"
            lines = [
                f"🔴 SELL SIGNAL — {sym} ({tf}) · {tag}",
                f"📌 Fresh {pool} pool start @ {_fmt_inr(pool_lvl)}",
                f"💵 Entry: {_fmt_inr(entry)} ({entry_note})",
                f"🛑 SL: {_fmt_inr(sl)}  ·  🎯 TP: {_fmt_inr(tp)} (1:2 R:R)",
            ]
            if target_str and target_str != "—":
                target_label = "Target pool" if abs(float(target) - pool_lvl) <= 0.011 else "Nearest pool"
                lines.append(f"🎯 {target_label}: {target_str}")
            if swing_time:
                st = swing_time if str(swing_time).endswith("IST") else f"{swing_time} IST"
                lines.append(f"📍 Chart Anchor (Swing {anchor_word.title()}): {st}")
            if speed == "fast":
                pct = round(100.0 * (4 - piv_len) / 4) if piv_len < 4 else 25
                lines.append(
                    f"⚡ Fast swing confirmed {piv_len} bars after actual {anchor_word} "
                    f"({pct}% faster, non-repainting)"
                )
            else:
                lines.append(
                    f"⚡ Swing confirmed {piv_len} bars after actual {anchor_word} (non-repainting)"
                )
            lines += [_bar_line(bar_time), _plain_bar(bar_time)]
            return kind, key, "\n".join(lines)

        elif "SWEEP_SSL" in action or "SSL_SWEPT" in action or ("SWEEP" in action and "SSL" in action):
            lvl = float(payload.get("pool_lvl", 0.0))
            close_px = payload.get("close")
            key = source_key(source, sym, tf, "SWEEP_SSL", bar_time, lvl)
            lines = [
                f"🧹 SSL SWEPT (Bullish Reclaim) — {sym} ({tf})",
                f"📈 Sell-side liquidity pool @ {_fmt_inr(lvl)} was swept — bullish (price reclaimed)",
            ]
            if close_px:
                lines.append(f"Close: {_fmt_inr(float(close_px))} > level {_fmt_inr(lvl)}")
            lines += [_bar_line(bar_time), _plain_bar(bar_time)]
            return "SWEEP_SSL", key, "\n".join(lines)

        elif "SWEEP_BSL" in action or "BSL_SWEPT" in action or ("SWEEP" in action and "BSL" in action):
            lvl = float(payload.get("pool_lvl", 0.0))
            close_px = payload.get("close")
            key = source_key(source, sym, tf, "SWEEP_BSL", bar_time, lvl)
            lines = [
                f"🧹 BSL SWEPT (Bearish Rejection) — {sym} ({tf})",
                f"📉 Buy-side liquidity pool @ {_fmt_inr(lvl)} was swept — bearish (price failed above)",
            ]
            if close_px:
                lines.append(f"Close: {_fmt_inr(float(close_px))} < level {_fmt_inr(lvl)}")
            lines += [_bar_line(bar_time), _plain_bar(bar_time)]
            return "SWEEP_BSL", key, "\n".join(lines)

        # Generic passthrough fallback. NOTE: Python's built-in hash() is
        # randomised per process, which would break dedupe across restarts —
        # use a stable digest instead.
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        key = f"{source}|{sym}|{tf}|GENERIC|{bar_time}|{digest}"
        return "GENERIC", key, f"🔔 TradingView Alert — {sym}\n{json.dumps(payload, indent=2)}"

    @staticmethod
    def _format_plaintext(text: str, default_sym: str = "NSE:NIFTY",
                          source: str = "TRADINGVIEW") -> tuple[str, str, str]:
        clean = text.strip()
        digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
        key = f"{str(source).upper()}|{normalize_symbol(default_sym)}|PLAINTEXT|{digest}"
        return "PLAINTEXT", key, clean


def _payload_pool_side(kind: str, payload: dict[str, Any]) -> str | None:
    explicit = str(payload.get("pool_side", "")).upper()
    if explicit in ("BSL", "SSL"):
        return explicit
    pool = str(payload.get("pool", "")).upper()
    if "BSL" in pool:
        return "BSL"
    if "SSL" in pool:
        return "SSL"
    if kind in ("INST_BUY", "SWEEP_SSL"):
        return "SSL"
    if kind in ("INST_SELL", "SWEEP_BSL"):
        return "BSL"
    return None


def _payload_details(kind: str, payload: dict[str, Any]) -> dict:
    """Extract comparable fields without trusting any source for identity."""
    if kind in ("BUY", "SELL", "FAST_BUY", "FAST_SELL"):
        return {
            "pool": payload.get("pool"),
            "pool_side": _payload_pool_side(kind, payload),
            "pool_lvl": payload.get("pool_lvl"),
            "entry": payload.get("entry", payload.get("close")),
            "sl": payload.get("sl"),
            "tp": payload.get("tp"),
            "target": payload.get("target"),
        }
    if kind in ("INST_BUY", "INST_SELL"):
        # Pine exposes the side and level for instant sweeps but not a stable
        # sequence name, so compare the canonical side/values only.
        return {
            "pool_side": _payload_pool_side(kind, payload),
            "pool_lvl": payload.get("pool_lvl"),
            "entry": payload.get("entry", payload.get("close")),
            "sl": payload.get("sl"),
            "tp": payload.get("tp"),
            "target": payload.get("target"),
        }
    if kind in ("SWEEP_SSL", "SWEEP_BSL"):
        return {
            "pool_side": _payload_pool_side(kind, payload),
            "pool_lvl": payload.get("pool_lvl"),
            "close": payload.get("close"),
        }
    return {}


def _annotate_source(text: str, status: dict) -> str:
    """Append provenance and cross-feed comparison status to a message."""
    lines = [text, "📡 Source: TRADINGVIEW"]
    relation = status.get("status")
    sources = ", ".join(status.get("sources", []))
    if relation == "confirmed":
        lines.append(f"✅ Cross-source confirmation: {sources}")
    elif relation == "conflict":
        fields = ", ".join(status.get("fields", [])) or "values"
        lines.append(f"⚠️ Source disagreement in: {fields} ({sources})")
    return "\n".join(lines)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_webhook_handler(notifier: TelegramNotifier, state: SentState, secret: str = "", default_sym: str = "NSE:NIFTY"):
    class WebhookHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Route HTTP server logs to standard logger
            log.debug("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)

        def do_GET(self):
            if self.path in ("/", "/health", "/status"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                resp = {
                    "status": "online",
                    "service": "BSL/SSL TradingView Webhook Receiver",
                    "bots_configured": len(notifier.bots),
                    "telegram_enabled": notifier.enabled,
                    "total_sent_alerts": len(state.sent),
                }
                self.wfile.write(json.dumps(resp, indent=2).encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            # Reject oversized bodies before reading (an unauthenticated
            # endpoint must not allow memory exhaustion).
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                content_length = 0
            if content_length <= 0 or content_length > MAX_BODY_BYTES:
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "invalid or too large body"}')
                return
            body = self.rfile.read(content_length).decode("utf-8", errors="replace")

            # Secret auth check if configured
            if secret:
                auth_hdr = self.headers.get("X-Webhook-Secret", "")
                if auth_hdr != secret and f"secret={secret}" not in self.path:
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Unauthorized"}')
                    return

            try:
                try:
                    payload = json.loads(body)
                except Exception:
                    payload = body

                # The webhook is always the TradingView source. Keep its
                # source-specific dedupe key separate from Yahoo's key, then
                # correlate the event so both messages expose confirmation or
                # a concrete value conflict instead of silently disagreeing.
                source = "TRADINGVIEW"
                kind, key, msg = WebhookFormatter.format_payload(
                    payload, default_sym, source=source
                )
                relation = {"status": "new", "sources": [source], "fields": []}
                if isinstance(payload, dict) and kind != "PLAINTEXT":
                    bar_time = payload.get("bar_time", "")
                    if bar_time:
                        correlation = event_correlation(
                            payload.get("symbol", default_sym),
                            payload.get("tf", "5m"), kind, bar_time,
                        )
                        relation = state.record_source_event(
                            correlation, source, _payload_details(kind, payload)
                        )
                msg = _annotate_source(msg, relation)

                legacy_key = key[len(source) + 1:] if key.startswith(source + "|") else key
                if not state.claim(key, (legacy_key,)):
                    log.info("Duplicate webhook alert ignored: %s", key)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status": "duplicate_ignored"}')
                    return

                try:
                    res = notifier.send(msg)
                except Exception:
                    state.release(key)
                    raise
                if res is False:
                    state.release(key)
                    # Delivery failed (and was queued nowhere on this path).
                    # Answer 5xx so TradingView retries the webhook; the key is
                    # NOT marked sent, so a successful retry cannot duplicate.
                    log.error("Webhook alert delivery FAILED (Telegram unreachable/"
                              "misconfigured): %s", key)
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status": "delivery_failed"}')
                    return

                state.mark(key)
                state.persist()
                log.info("Webhook alert delivered to Telegram [%s]: %s", kind, key)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "delivered"}')

            except Exception as e:
                log.exception("Error processing webhook POST")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    return WebhookHandler


class WebhookServer:
    """Zero-delay HTTP server receiving real-time TradingView Webhook alerts."""

    def __init__(self, cfg, notifier: TelegramNotifier | None = None, state: SentState | None = None):
        self.cfg = cfg
        self.host = getattr(cfg, "webhook_host", "0.0.0.0")
        self.port = int(getattr(cfg, "webhook_port", 5000))
        self.secret = getattr(cfg, "webhook_secret", "")
        self.state = state or SentState(cfg.state_file)
        self.notifier = notifier or TelegramNotifier(
            bot1_token=cfg.bot1_token, bot2_token=cfg.bot2_token,
            chat_id=cfg.chat_id, chat_id_2=cfg.chat_id_2,
            enabled=cfg.telegram_enabled,
        )
        handler_cls = make_webhook_handler(self.notifier, self.state, self.secret, cfg.display_symbol)
        self.httpd = ThreadedHTTPServer((self.host, self.port), handler_cls)

    def run_forever(self):
        if not self.secret:
            log.warning(
                "⚠️  WEBHOOK_SECRET is NOT set — the endpoint at %s:%s is UNAUTHENTICATED. "
                "Anyone who reaches it can inject arbitrary/fake signals into your Telegram. "
                "Set WEBHOOK_SECRET in .env and use http://<host>:%s/webhook?secret=<value> "
                "in TradingView (or send the X-Webhook-Secret header).",
                self.host, self.port, self.port,
            )
        log.info("TradingView Webhook Server listening on http://%s:%d/webhook (Zero Delay)", self.host, self.port)
        log.info("Direct dual Telegram alerts active: %s bots ready", len(self.notifier.bots))
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("Shutting down webhook server...")
            self.httpd.shutdown()
            self.state.persist()
