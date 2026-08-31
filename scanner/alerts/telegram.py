"""Dual-bot Telegram notifier.

Both Telegram bots receive the exact same message text, so a signal reaches
you even if one bot fails. Sending is considered successful when at least one
bot delivers; failed sends are left unmarked so they are retried on the next
scan cycle (never silently dropped, never duplicated once delivered).
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, bot1_token: str = "", bot2_token: str = "",
                 chat_id: str = "", chat_id_2: str = "",
                 enabled: bool = True, timeout: int = 12):
        self.enabled = enabled
        self.timeout = timeout
        self.bots = []
        if bot1_token and chat_id:
            self.bots.append(("bot1", bot1_token, chat_id))
        if bot2_token and (chat_id_2 or chat_id):
            self.bots.append(("bot2", bot2_token, chat_id_2 or chat_id))

    # ------------------------------------------------------------------
    def send(self, text: str) -> bool | None:
        """Send `text` via both bots.

        Returns:
          True  -> delivered by at least one bot
          None  -> telegram disabled / no bots configured (dry-run: log only)
          False -> configured but delivery failed (caller should retry later)
        """
        if not self.enabled:
            log.info("[dry-run] (telegram disabled) alert:\n%s", text)
            return None
        if not self.bots:
            log.warning("No Telegram bots configured (set BOT1_TOKEN/BOT2_TOKEN + CHAT_ID in .env) — alert logged only:\n%s", text)
            return None

        delivered = 0
        for name, token, cid in self.bots:
            try:
                r = requests.post(
                    API_BASE.format(token=token),
                    json={"chat_id": cid, "text": text,
                          "disable_web_page_preview": True, "disable_notification": False},
                    timeout=self.timeout,
                )
                if r.status_code == 200 and r.json().get("ok"):
                    delivered += 1
                else:
                    log.error("[%s] Telegram API error %s: %s", name, r.status_code, r.text[:300])
            except Exception:
                log.exception("[%s] Telegram send failed", name)
        return delivered > 0
