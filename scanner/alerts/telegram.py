"""Dual-bot Telegram notifier.

Both Telegram bots receive the exact same message text, so a signal reaches
you even if one bot fails. Delivery is tracked PER BOT: if one bot succeeds
and the other fails, only the failed bot is retried on later scan cycles —
the successful one is never spammed with a duplicate, and the failed one is
never silently dropped. An alert is considered fully sent only when every
configured bot has acknowledged it.
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
    @property
    def bot_names(self) -> list[str]:
        return [name for name, _, _ in self.bots]

    def send(self, text: str, only: list[str] | None = None) -> dict | None:
        """Send `text` to the selected bots (default: all configured bots).

        Parameters
        ----------
        only : optional list of bot names to send to (used to retry only the
               bots that failed a previous delivery, avoiding duplicates).

        Returns
        -------
        dict[str, bool]  -> per-bot delivery result ({'bot1': True, ...})
        None             -> telegram disabled / no bots configured
                            (dry-run: message logged only, never retried)
        """
        if not self.enabled:
            log.info("[dry-run] (telegram disabled) alert:\n%s", text)
            return None
        if not self.bots:
            log.warning("No Telegram bots configured (set BOT1_TOKEN/BOT2_TOKEN + CHAT_ID in .env) — alert logged only:\n%s", text)
            return None

        targets = self.bots if only is None else [b for b in self.bots if b[0] in only]
        results: dict[str, bool] = {}
        for name, token, cid in targets:
            try:
                r = requests.post(
                    API_BASE.format(token=token),
                    json={"chat_id": cid, "text": text,
                          "disable_web_page_preview": True, "disable_notification": False},
                    timeout=self.timeout,
                )
                ok = r.status_code == 200 and bool(r.json().get("ok"))
                if not ok:
                    log.error("[%s] Telegram API error %s: %s", name, r.status_code, r.text[:300])
                results[name] = ok
            except Exception:
                log.exception("[%s] Telegram send failed", name)
                results[name] = False
        return results
