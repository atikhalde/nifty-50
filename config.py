"""Scanner configuration — reads secrets/settings from environment (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # loads .env from the repo root


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: list[str]) -> list[str]:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return [x.strip() for x in v.split(",") if x.strip()]


@dataclass
class ScannerConfig:
    # --- symbol / universe (NSE:NIFTY only) ---
    symbol: str = os.getenv("SYMBOL", "^NSEI")
    display_symbol: str = os.getenv("DISPLAY_SYMBOL", "NSE:NIFTY")

    # --- timeframes ---
    timeframes: list[str] = field(default_factory=lambda: _env_list("TIMEFRAMES", ["1m", "5m"]))

    # --- scanning cadence ---
    scan_interval_sec: int = int(os.getenv("SCAN_INTERVAL_SEC", "20"))
    market_hours_only: bool = _env_bool("MARKET_HOURS_ONLY", True)
    session_start: str = os.getenv("SESSION_START", "09:15")
    session_end: str = os.getenv("SESSION_END", "15:30")
    tz: str = os.getenv("TZ", "Asia/Kolkata")

    # --- lookback mode (GitHub Actions / cloud) ---
    # Each Actions run is a fresh machine: scan the last N minutes of closed
    # bars every run instead of waiting for new bars. Dedupe still comes from
    # the cached data/sent_alerts.json, so no alert is ever repeated.
    # 0 = disabled (live incremental mode).
    lookback_minutes: int = int(os.getenv("LOOKBACK_MINUTES", "0") or 0)

    # --- telegram (both bots receive the SAME alerts) ---
    telegram_enabled: bool = _env_bool("TELEGRAM_ENABLED", True)
    bot1_token: str = os.getenv("BOT1_TOKEN", "")
    bot2_token: str = os.getenv("BOT2_TOKEN", "")
    chat_id: str = os.getenv("CHAT_ID", "")
    chat_id_2: str = os.getenv("CHAT_ID_2", "") or os.getenv("CHAT_ID", "")

    # --- alert options ---
    sweep_alerts: bool = _env_bool("SWEEP_ALERTS", True)

    # --- state / logging ---
    state_file: str = os.getenv("STATE_FILE", "data/sent_alerts.json")
    log_file: str = os.getenv("LOG_FILE", "logs/scanner.log")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
