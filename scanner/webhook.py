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
    def format_payload(payload: dict[str, Any] | str, default_sym: str = "NSE:NIFTY") -> tuple[str, str, str]:
        """Returns (kind, dedupe_key, formatted_telegram_message)."""
        if isinstance(payload, str):
            payload = payload.strip()
            if payload.startswith("{") and payload.endswith("}"):
                try:
                    payload = json.loads(payload)
                except Exception:
                    return WebhookFormatter._format_plaintext(payload, default_sym)
            else:
                return WebhookFormatter._format_plaintext(payload, default_sym)

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
            piv_len = int(payload.get("piv_len", 3 if speed == "fast" else (0 if speed == "instant" else 8)))
        except (TypeError, ValueError):
            piv_len = 8 if speed == "standard" else (3 if speed == "fast" else 0)

        sym = payload.get("symbol", default_sym)
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
            key = f"{sym}|{tf}|INST_BUY|{bar_time}|{round(pool_lvl, 2)}"
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
            key = f"{sym}|{tf}|INST_SELL|{bar_time}|{round(pool_lvl, 2)}"
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
            pool_lvl = float(payload.get("pool_lvl", 0.0))
            entry = float(payload.get("entry", 0.0))
            sl = float(payload.get("sl", 0.0))
            tp = float(payload.get("tp", 0.0))
            target = payload.get("target")
            target_str = _fmt_inr(float(target)) if target is not None and target != "" else None
            kind = "FAST_BUY" if speed == "fast" else "BUY"
            tag = "FAST" if speed == "fast" else "STANDARD"
            key = f"{sym}|{tf}|{kind}|{bar_time}|{round(pool_lvl, 2)}"
            lines = [
                f"🟢 BUY SIGNAL — {sym} ({tf}) · {tag}",
                f"📌 Fresh {pool} pool start @ {_fmt_inr(pool_lvl)}",
                f"💵 Entry: {_fmt_inr(entry)} (Closed Bar)",
                f"🛑 SL: {_fmt_inr(sl)}  ·  🎯 TP: {_fmt_inr(tp)} (1:2 R:R)",
            ]
            if target_str and target_str != "—":
                lines.append(f"🎯 Nearest Target Pool: {target_str}")
            if swing_time:
                st = swing_time if str(swing_time).endswith("IST") else f"{swing_time} IST"
                lines.append(f"📍 Chart Anchor (Swing Low): {st}")
            if speed == "fast":
                pct = round(100.0 * (8 - piv_len) / 8) if piv_len < 8 else 62
                lines.append(
                    f"⚡ Fast swing confirmed {piv_len} bars after actual LOW "
                    f"({pct}% faster, non-repainting)"
                )
            else:
                lines.append(
                    f"⚡ Swing confirmed {piv_len} bars after actual LOW (non-repainting)"
                )
            lines += [_bar_line(bar_time), _plain_bar(bar_time)]
            return kind, key, "\n".join(lines)

        elif "SELL" in action and "SWEEP" not in action:
            pool = payload.get("pool", "BSL")
            pool_lvl = float(payload.get("pool_lvl", 0.0))
            entry = float(payload.get("entry", 0.0))
            sl = float(payload.get("sl", 0.0))
            tp = float(payload.get("tp", 0.0))
            target = payload.get("target")
            target_str = _fmt_inr(float(target)) if target is not None and target != "" else None
            kind = "FAST_SELL" if speed == "fast" else "SELL"
            tag = "FAST" if speed == "fast" else "STANDARD"
            key = f"{sym}|{tf}|{kind}|{bar_time}|{round(pool_lvl, 2)}"
            lines = [
                f"🔴 SELL SIGNAL — {sym} ({tf}) · {tag}",
                f"📌 Fresh {pool} pool start @ {_fmt_inr(pool_lvl)}",
                f"💵 Entry: {_fmt_inr(entry)} (Closed Bar)",
                f"🛑 SL: {_fmt_inr(sl)}  ·  🎯 TP: {_fmt_inr(tp)} (1:2 R:R)",
            ]
            if target_str and target_str != "—":
                lines.append(f"🎯 Nearest Target Pool: {target_str}")
            if swing_time:
                st = swing_time if str(swing_time).endswith("IST") else f"{swing_time} IST"
                lines.append(f"📍 Chart Anchor (Swing High): {st}")
            if speed == "fast":
                pct = round(100.0 * (8 - piv_len) / 8) if piv_len < 8 else 62
                lines.append(
                    f"⚡ Fast swing confirmed {piv_len} bars after actual HIGH "
                    f"({pct}% faster, non-repainting)"
                )
            else:
                lines.append(
                    f"⚡ Swing confirmed {piv_len} bars after actual HIGH (non-repainting)"
                )
            lines += [_bar_line(bar_time), _plain_bar(bar_time)]
            return kind, key, "\n".join(lines)

        elif "SWEEP_SSL" in action or "SSL_SWEPT" in action or ("SWEEP" in action and "SSL" in action):
            lvl = float(payload.get("pool_lvl", 0.0))
            close_px = payload.get("close")
            key = f"{sym}|{tf}|SWEEP_SSL|{bar_time}|{round(lvl, 2)}"
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
            key = f"{sym}|{tf}|SWEEP_BSL|{bar_time}|{round(lvl, 2)}"
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
        key = f"{sym}|{tf}|GENERIC|{bar_time}|{digest}"
        return "GENERIC", key, f"🔔 TradingView Alert — {sym}\n{json.dumps(payload, indent=2)}"

    @staticmethod
    def _format_plaintext(text: str, default_sym: str = "NSE:NIFTY") -> tuple[str, str, str]:
        clean = text.strip()
        digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
        key = f"{default_sym}|PLAINTEXT|{digest}"
        return "PLAINTEXT", key, clean


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

                kind, key, msg = WebhookFormatter.format_payload(payload, default_sym)

                if state.already_sent(key):
                    log.info("Duplicate webhook alert ignored: %s", key)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status": "duplicate_ignored"}')
                    return

                res = notifier.send(msg)
                if res is False:
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
