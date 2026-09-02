"""Persistent alert dedupe + per-timeframe progress tracking.

Guarantees the "do not repeat any alert twice" requirement per source, even
across scanner restarts: every source alert has a unique key and is only sent
once. A shared, locked ledger also correlates Yahoo and TradingView events.
"""

from __future__ import annotations

import json
import logging
import os
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

from scanner.parity import clean_details, differing_fields

log = logging.getLogger(__name__)


class SentState:
    # A full trading day emits far fewer than this; the cap only guards
    # against unbounded growth over months of continuous running.
    MAX_KEYS = 20_000
    PRUNE_SLACK = 2_000
    MAX_SOURCE_EVENTS = 20_000

    def __init__(self, path: str):
        self.path = path
        self._mutex = threading.RLock()
        self._inflight: set[str] = set()
        self.sent: dict[str, bool] = {}
        self.last_evaluated: dict[str, str] = {}
        # alerts that failed delivery and must be retried:
        #   key -> {"text": str, "queued": iso-timestamp}
        self.pending: dict[str, dict] = {}
        # Cross-source correlation ledger. The key deliberately excludes the
        # source and level; this is how a TradingView event and a Yahoo event
        # at the same bar are classified as confirmed or conflicting.
        self.source_events: dict[str, dict[str, dict]] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.sent = d.get("sent", {})
            self.last_evaluated = d.get("last_evaluated", {})
            self.pending = d.get("pending", {})
            self.source_events = d.get("source_events", {})
            if not isinstance(self.source_events, dict):
                self.source_events = {}
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Could not load state file %s — starting fresh", self.path)

    def already_sent(self, key: str) -> bool:
        with self._mutex:
            return key in self.sent

    def claim(self, key: str, aliases: tuple[str, ...] = ()) -> bool:
        """Atomically reserve a key before an outbound send.

        This closes the small same-process race where two webhook worker
        threads receive the same alert before either one can call ``mark``.
        A failed send must call ``release`` so the source can retry.
        """
        with self._mutex:
            if key in self.sent or key in self._inflight:
                return False
            if any(alias in self.sent for alias in aliases):
                return False
            self._inflight.add(key)
            return True

    def release(self, key: str) -> None:
        with self._mutex:
            self._inflight.discard(key)

    def mark(self, key: str) -> None:
        with self._mutex:
            self._inflight.discard(key)
            self.sent[key] = True
            # An alert that has been delivered no longer needs retrying.
            self.pending.pop(key, None)
            self._prune()

    # ------------------------------------------------------------------
    def add_pending(self, key: str, text: str, queued_iso: str) -> None:
        """Queue an alert whose delivery failed, for retry on a later cycle.

        An alert that was already delivered is never re-queued — the strict
        no-duplicate guarantee wins over re-delivery.
        """
        with self._mutex:
            if key in self.sent:
                return
            self.pending[key] = {"text": text, "queued": queued_iso}

    def drop_pending(self, key: str) -> None:
        """Discard a pending alert (e.g. it aged out past PENDING_MAX_AGE_MIN)."""
        with self._mutex:
            self.pending.pop(key, None)

    def record_source_event(self, correlation_key: str, source: str,
                            details: dict) -> dict:
        """Record an event and classify its relationship to another source.

        Returns a small status object used only to annotate the alert:
        ``new``, ``same_source``, ``confirmed`` or ``conflict``.  The source
        key remains separate from ``self.sent``: both feeds are allowed to
        deliver their own alert, but the user can see whether their numeric
        values agree.
        """
        source = str(source).upper()
        cleaned = clean_details(details)
        with self._mutex:
            bucket = self.source_events.setdefault(correlation_key, {})
            prior_sources = sorted(bucket)
            prior_details = [bucket[s] for s in prior_sources if s != source]
            conflicts = []
            for previous in prior_details:
                conflicts.extend(differing_fields(previous, cleaned))
            conflicts = sorted(set(conflicts))
            bucket[source] = cleaned
            self._prune_source_events()
            if source in prior_sources:
                status = "same_source"
            elif not prior_sources:
                status = "new"
            else:
                status = "conflict" if conflicts else "confirmed"
            return {"status": status, "sources": sorted(bucket), "fields": conflicts}

    def _prune_source_events(self) -> None:
        overflow = len(self.source_events) - self.MAX_SOURCE_EVENTS
        if overflow <= 0:
            return
        for key in list(self.source_events)[:overflow + self.PRUNE_SLACK]:
            self.source_events.pop(key, None)

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
        with self._mutex:
            self.last_evaluated[tf] = ts_iso

    def persist(self) -> None:
        """Atomically persist, merging updates from a concurrent source process.

        The Yahoo scanner and TradingView webhook listener are commonly run as
        two processes against one state file. An exclusive lock plus a
        read/merge/write cycle prevents one process from erasing the other
        process's sent keys or correlation bucket.
        """
        self._mutex.acquire()
        try:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                lock_path = self.path + ".lock"
                with open(lock_path, "a+", encoding="utf-8") as lock:
                    if fcntl is not None:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    try:
                        disk = {}
                        try:
                            with open(self.path, "r", encoding="utf-8") as f:
                                loaded = json.load(f)
                            disk = loaded if isinstance(loaded, dict) else {}
                        except FileNotFoundError:
                            pass
                        except Exception:
                            log.warning("Could not merge existing state %s; preserving local state",
                                        self.path, exc_info=True)

                        disk_sent = disk.get("sent", {})
                        if not isinstance(disk_sent, dict):
                            disk_sent = {}
                        self.sent = {**disk_sent, **self.sent}

                        disk_last = disk.get("last_evaluated", {})
                        if not isinstance(disk_last, dict):
                            disk_last = {}
                        merged_last = dict(disk_last)
                        for tf, ts in self.last_evaluated.items():
                            if tf not in merged_last or str(ts) > str(merged_last[tf]):
                                merged_last[tf] = ts
                        self.last_evaluated = merged_last

                        disk_pending = disk.get("pending", {})
                        if not isinstance(disk_pending, dict):
                            disk_pending = {}
                        self.pending = {**disk_pending, **self.pending}
                        for key in self.sent:
                            self.pending.pop(key, None)

                        disk_events = disk.get("source_events", {})
                        if not isinstance(disk_events, dict):
                            disk_events = {}
                        merged_events = {}
                        for events in (disk_events, self.source_events):
                            if not isinstance(events, dict):
                                continue
                            for correlation, bucket in events.items():
                                if not isinstance(bucket, dict):
                                    continue
                                target = merged_events.setdefault(correlation, {})
                                target.update({source: details for source, details in bucket.items()
                                               if isinstance(details, dict)})
                        self.source_events = merged_events
                        self._prune()
                        self._prune_source_events()

                        tmp = self.path + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump({"sent": self.sent,
                                       "last_evaluated": self.last_evaluated,
                                       "pending": self.pending,
                                       "source_events": self.source_events},
                                      f, indent=2)
                        os.replace(tmp, self.path)
                    finally:
                        if fcntl is not None:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except Exception:
                log.exception("Could not persist state to %s", self.path)
        finally:
            self._mutex.release()
