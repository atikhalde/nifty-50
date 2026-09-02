#!/usr/bin/env python3
"""Trim old alert keys and source events from the cached state file.

Source-specific alert keys embed the bar timestamp as:
  source|symbol|tf|kind|YYYY-mm-dd HH:MM|level
Legacy keys without the source prefix are also accepted. Entries older than
``--max-age-days`` are dropped; progress markers are always preserved.

Usage:
    python scanner/prune_state.py [--state data/sent_alerts.json] [--max-age-days 14]

Idempotent, and a no-op when the state file does not exist or is unreadable.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


EXCHANGE_TZ = ZoneInfo("Asia/Kolkata")


def _parse_local_or_aware(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EXCHANGE_TZ)
    return parsed


def _key_time(key: str) -> datetime | None:
    """Parse a source-specific or legacy timestamp embedded in an alert key."""
    try:
        parts = key.split("|")
        # New: source|symbol|tf|kind|bar-time|level
        # Legacy: symbol|tf|kind|bar-time|level
        timestamp = parts[4] if len(parts) >= 6 else parts[3]
        return _parse_local_or_aware(timestamp)
    except Exception:
        return None


def _event_time(key: str) -> datetime | None:
    """Parse the local bar time at the end of a source correlation key."""
    try:
        return _parse_local_or_aware(key.rsplit("|", 1)[1])
    except Exception:
        return None


def prune(path: str, max_age_days: int, max_keys: int = 10_000) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return False
    except Exception:
        print(f"prune: could not read {path} — leaving it untouched")
        return False

    sent: dict = d.get("sent", {}) or {}
    if not isinstance(sent, dict):
        sent = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    kept = {}
    removed = 0
    for key, val in sent.items():
        t = _key_time(str(key))
        if t is None or t.astimezone(timezone.utc) >= cutoff:
            kept[key] = val
        else:
            removed += 1

    # hard cap: keep the newest `max_keys` entries
    capped = 0
    if len(kept) > max_keys:
        order = sorted(kept, key=lambda k: _key_time(str(k)) or datetime.min.replace(tzinfo=timezone.utc))
        for key in order[: len(kept) - max_keys]:
            del kept[key]
            capped += 1

    events: dict = d.get("source_events", {}) or {}
    if not isinstance(events, dict):
        events = {}
    kept_events = {}
    events_removed = 0
    for key, bucket in events.items():
        t = _event_time(str(key))
        if t is None or t.astimezone(timezone.utc) >= cutoff:
            kept_events[key] = bucket
        else:
            events_removed += 1

    d["sent"] = kept
    d["source_events"] = kept_events
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)
    print(f"prune: {path}: {len(sent)} -> {len(kept)} keys "
          f"(-{removed} old, -{capped} over cap), "
          f"source events {len(events)} -> {len(kept_events)} "
          f"(-{events_removed} old)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Trim old alert keys from the dedupe state")
    ap.add_argument("--state", default="data/sent_alerts.json")
    ap.add_argument("--max-age-days", type=int, default=14)
    args = ap.parse_args()
    prune(args.state, max(0, args.max_age_days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
