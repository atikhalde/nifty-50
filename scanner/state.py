"""Persistent alert dedupe + per-timeframe progress tracking.

Guarantees the "do not repeat any alert twice" requirement, even across
scanner restarts: every alert has a unique key and is only sent once.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)


class SentState:
    # A full trading day emits far fewer than this; the cap only guards
    # against unbounded growth over months of continuous running.
    MAX_KEYS = 20_000
    PRUNE_SLACK = 2_000

    def __init__(self, path: str):
        self.path = path
        self.sent: dict[str, bool] = {}
        self.last_evaluated: dict[str, str] = {}
        # alerts that failed delivery and must be retried:
        #   key -> {"text": str, "queued": iso-timestamp}
        self.pending: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.sent = d.get("sent", {})
            self.last_evaluated = d.get("last_evaluated", {})
            self.pending = d.get("pending", {})
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Could not load state file %s — starting fresh", self.path)

    def already_sent(self, key: str) -> bool:
        return key in self.sent

    def mark(self, key: str) -> None:
        self.sent[key] = True
        self._prune()

    def _prune(self) -> None:
        """Keep the dedupe ledger bounded.

        Keys are appended in chronological order, so dropping the oldest
        entries is safe: those bars can never be re-emitted anyway because
        `last_evaluated` has long since moved past them.
        """
        overflow = len(self.sent) - self.MAX_KEYS
        if overflow <= 0:
            return
        for key in list(self.sent)[:overflow + self.PRUNE_SLACK]:
            self.sent.pop(key, None)
        log.info("Pruned dedupe ledger to %d keys", len(self.sent))

    def set_last_evaluated(self, tf: str, ts_iso: str) -> None:
        self.last_evaluated[tf] = ts_iso

    def persist(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"sent": self.sent, "last_evaluated": self.last_evaluated,
                           "pending": self.pending},
                          f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            log.exception("Could not persist state to %s", self.path)
