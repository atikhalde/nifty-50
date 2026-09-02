"""Shared event identity and source annotations for TradingView/Yahoo parity.

TradingView and Yahoo are deliberately kept as separate alert sources.  A
source-specific dedupe key prevents a retry from duplicating that source,
while the correlation key lets the state ledger tell the user when both
sources saw the same event or when their values disagree.
"""

from __future__ import annotations

import math

import pandas as pd


EXCHANGE_TZ = "Asia/Kolkata"


def normalize_symbol(value: object, default: str = "NSE:NIFTY") -> str:
    """Normalize the common TradingView/Yahoo names for the NIFTY index."""
    text = str(value or default).strip()
    upper = text.upper()
    if upper in {"NIFTY", "NSE:NIFTY", "^NSEI", "NIFTY 50", "NIFTY50"}:
        return "NSE:NIFTY"
    return text


def normalize_time(value: object, tz: str = EXCHANGE_TZ) -> str:
    """Return a comparable local bar time in ``YYYY-mm-dd HH:MM`` form."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.upper().endswith("IST"):
        text = text[:-3].strip()
    ts = pd.Timestamp(text)
    zone = tz
    if ts.tzinfo is None or ts.tz is None:
        ts = ts.tz_localize(zone)
    else:
        ts = ts.tz_convert(zone)
    return ts.strftime("%Y-%m-%d %H:%M")


def event_correlation(symbol: object, timeframe: object, kind: str,
                      bar_time: object, tz: str = EXCHANGE_TZ) -> str:
    """Identity shared by both feeds for a signal at the same bar."""
    return "|".join((normalize_symbol(symbol), str(timeframe), str(kind),
                     normalize_time(bar_time, tz)))


def source_key(source: str, symbol: object, timeframe: object, kind: str,
               bar_time: object, level: object,
               tz: str = EXCHANGE_TZ) -> str:
    """Strict per-source dedupe key; a source can safely retry independently."""
    level_text = "na"
    try:
        number = float(level)
        if math.isfinite(number):
            level_text = str(round(number, 4))
    except (TypeError, ValueError):
        pass
    return "|".join((str(source).upper(), event_correlation(symbol, timeframe, kind,
                                                              bar_time, tz), level_text))


def clean_details(details: dict) -> dict:
    """Make event details JSON-safe and stable for cross-source comparison."""
    cleaned = {}
    for key, value in details.items():
        if value is None:
            cleaned[key] = None
            continue
        try:
            number = float(value)
            cleaned[key] = round(number, 4) if math.isfinite(number) else None
        except (TypeError, ValueError):
            cleaned[key] = str(value)
    return cleaned


def differing_fields(left: dict, right: dict, tolerance: float = 0.011) -> list[str]:
    """Return numeric/detail fields that disagree beyond one NIFTY tick."""
    fields = sorted(set(left) | set(right))
    differences = []
    for field in fields:
        a, b = left.get(field), right.get(field)
        if a is None or b is None:
            if a != b:
                differences.append(field)
            continue
        try:
            if abs(float(a) - float(b)) > tolerance:
                differences.append(field)
        except (TypeError, ValueError):
            if str(a) != str(b):
                differences.append(field)
    return differences
