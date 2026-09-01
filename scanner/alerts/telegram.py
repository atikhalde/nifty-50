"""Dual-bot Telegram notifier.

Both Telegram bots receive the exact same message text, so a signal reaches
you even if one bot fails. Sending is considered successful when at least one
bot delivers; failed sends are left unmarked so they are retried on the next
scan cycle (never silently dropped, never duplicated once delivered).
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram's hard limit for a single text message.
MAX_TEXT = 4096


class TelegramNotifier:
    def __init__(self, bot1_token: str = "", bot2_token: str = "",
                 chat_id: str = "", chat_id_2: str = "",
                 enabled: bool = True, timeout: int = 12, max_retries: int = 3):
        self.enabled = enabled
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self._session = requests.Session()
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
          None  -> explicitly disabled (dry-run: log only, safe to mark sent)
          False -> delivery failed OR no bots configured (caller must NOT mark
                   the alert as sent, otherwise credentials added later would
                   never receive it)
        """
        if not self.enabled:
            log.info("[dry-run] (telegram disabled) alert:\n%s", text)
            return None
        if not self.bots:
            log.warning("No Telegram bots configured (set BOT1_TOKEN/BOT2_TOKEN + "
                        "CHAT_ID in .env) — alert logged, NOT marked as sent:\n%s", text)
            return False

        # Telegram hard-limits a message to 4096 characters.
        if len(text) > MAX_TEXT:
            log.warning("Alert text %d chars — truncating to %d", len(text), MAX_TEXT)
            text = text[:MAX_TEXT - 1] + "…"

        delivered = 0
        for name, token, cid in self.bots:
            if self._send_one(name, token, cid, text):
                delivered += 1
        return delivered > 0

    # ------------------------------------------------------------------
    def _send_one(self, name: str, token: str, cid: str, text: str) -> bool:
        payload = {"chat_id": cid, "text": text,
                   "disable_web_page_preview": True, "disable_notification": False}

        for attempt in range(1, self.max_retries + 1):
            try:
                r = self._session.post(API_BASE.format(token=token),
                                       json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("[%s] Telegram send failed (attempt %d/%d): %s",
                            name, attempt, self.max_retries, e)
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                continue

            if r.status_code == 200:
                try:
                    if r.json().get("ok"):
                        return True
                except ValueError:
                    log.error("[%s] Telegram returned non-JSON body: %s", name, r.text[:200])
                log.error("[%s] Telegram rejected the message: %s", name, r.text[:300])
                return False

            if r.status_code == 429:
                # Rate limited — honour retry_after rather than hammering.
                wait = self.timeout
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", wait))
                except (ValueError, AttributeError, TypeError):
                    pass
                wait = min(max(wait, 1), 60)
                log.warning("[%s] Telegram rate limited — waiting %ss", name, wait)
                if attempt < self.max_retries:
                    time.sleep(wait)
                continue

            if r.status_code in (401, 403, 400, 404):
                # Bad token / wrong chat / bot blocked: retrying cannot help.
                log.error("[%s] Telegram config error %s (check token & CHAT_ID): %s",
                          name, r.status_code, r.text[:300])
                return False

            log.error("[%s] Telegram API error %s: %s", name, r.status_code, r.text[:300])
            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 8))

        return False

    # ------------------------------------------------------------------
    def check_credentials(self) -> bool:
        """Ping getMe/getChat at startup so bad tokens surface before the open."""
        if not self.enabled or not self.bots:
            return False
        all_ok = True
        for name, token, cid in self.bots:
            try:
                r = self._session.get(
                    f"https://api.telegram.org/bot{token}/getMe", timeout=self.timeout)
                ok = r.status_code == 200 and r.json().get("ok")
                if ok:
                    uname = r.json().get("result", {}).get("username", "?")
                    log.info("[%s] Telegram OK — @%s -> chat %s", name, uname, cid)
                else:
                    all_ok = False
                    log.error("[%s] Telegram token check FAILED (%s): %s",
                              name, r.status_code, r.text[:200])
            except Exception as e:  # noqa: BLE001
                all_ok = False
                log.error("[%s] Telegram token check failed: %s", name, e)
        return all_ok
