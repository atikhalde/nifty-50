#!/usr/bin/env python3
"""Trim old alert keys from data/sent_alerts.json (cache hygiene).

Alert keys embed the bar timestamp:  symbol|tf|kind|2026-08-31T14:35:00+05:30|lvl
Keys older than `--max-age-days` are dropped; `last_evaluated` progress markers
are always preserved. A hard cap on the number of keys keeps the GitHub cache
entry tiny even if alerts fire very often.

Usage:
    python scanner/prune_state.py [--state data/sent_alerts.json] [--max-age-days 14]

Idempotent, and a no-op when the state file does not exist or is unreadable.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone


def _key_time(key: str) -> datetime | None:
    """Parse the timestamp embedded in an alert key, or None."""
    try:
        return datetime.fromisoformat(key.split("|")[3])
    except Exception:
        return None


def prune(path: str, max_age_days: int, max_keys: int = 10_000) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        print(f"prune: could not read {path} — leaving it untouched")
        return False

    sent: dict = d.get("sent", {}) or {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    kept = {}
    removed = 0
    for key, val in sent.items():
        t = _key_time(key)
        if t is None or t >= cutoff:
            kept[key] = val
        else:
            removed += 1

    # hard cap: keep the newest `max_keys` entries
    capped = 0
    if len(kept) > max_keys:
        order = sorted(kept, key=lambda k: _key_time(k) or datetime.min.replace(tzinfo=timezone.utc))
        for key in order[: len(kept) - max_keys]:
            del kept[key]
            capped += 1

    d["sent"] = kept
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)
    print(f"prune: {path}: {len(sent)} -> {len(kept)} keys "
          f"(-{removed} old, -{capped} over cap)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Trim old alert keys from the dedupe state")
    ap.add_argument("--state", default="data/sent_alerts.json")
    ap.add_argument("--max-age-days", type=int, default=14)
    args = ap.parse_args()
    prune(args.state, args.max_age_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
