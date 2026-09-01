"""Scanner configuration — reads secrets/settings from environment (.env).

Every value is parsed defensively: a typo in `.env` must never crash the
scanner during market hours. Bad values fall back to the documented default
and are reported by `ScannerConfig.validate()` at startup.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent

load_dotenv(REPO_ROOT / ".env")  # loads .env from the repo root, whatever the cwd

log = logging.getLogger("scanner.config")

# Timeframes the yfinance feed supports.
SUPPORTED_TIMEFRAMES = ("1m", "2m", "5m", "15m", "30m", "60m", "1h", "1d")


def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if v is not None and v.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    s = v.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    log.warning("%s=%r is not a boolean — using default %s", name, v, default)
    return default


def _env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    try:
        n = int(float(v.strip()))
    except ValueError:
        log.warning("%s=%r is not a number — using default %s", name, v, default)
        return default
    if lo is not None and n < lo:
        log.warning("%s=%s below minimum %s — clamping", name, n, lo)
        n = lo
    if hi is not None and n > hi:
        log.warning("%s=%s above maximum %s — clamping", name, n, hi)
        n = hi
    return n


def _env_list(name: str, default: list[str]) -> list[str]:
    v = os.getenv(name)
    if v is None or not v.strip():
        return list(default)
    items, seen = [], set()
    for x in v.split(","):
        x = x.strip()
        if not x or x in seen:
            continue
        seen.add(x)
        items.append(x)
    return items or list(default)


def _env_tz(default: str = "Asia/Kolkata") -> str:
    """Exchange timezone.

    SCANNER_TZ wins over TZ: `TZ` is a standard POSIX variable that container
    images and CI runners often set to UTC, which would silently shift the
    whole NSE session window.
    """
    name = _env_str("SCANNER_TZ", "") or _env_str("TZ", default)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("Unknown timezone %r — falling back to %s", name, default)
        return default
    return name


def _env_time(name: str, default: str) -> str:
    """HH:MM string, tolerant of '9:15' and validated against the clock."""
    raw = _env_str(name, default)
    for candidate in (raw, raw.zfill(5) if len(raw) == 4 and ":" in raw else raw):
        try:
            dtime.fromisoformat(candidate)
            return candidate
        except ValueError:
            continue
    # last resort: pad a single-digit hour, e.g. "9:15" -> "09:15"
    if ":" in raw:
        h, _, m = raw.partition(":")
        padded = f"{h.strip().zfill(2)}:{m.strip().zfill(2)}"
        try:
            dtime.fromisoformat(padded)
            return padded
        except ValueError:
            pass
    log.warning("%s=%r is not a valid HH:MM time — using default %s", name, raw, default)
    return default


def _resolve(path: str) -> str:
    """Make relative state/log paths independent of the current directory."""
    p = Path(path).expanduser()
    return str(p if p.is_absolute() else REPO_ROOT / p)


@dataclass
class ScannerConfig:
    # --- symbol / universe (NSE:NIFTY only) ---
    symbol: str = field(default_factory=lambda: _env_str("SYMBOL", "^NSEI"))
    display_symbol: str = field(default_factory=lambda: _env_str("DISPLAY_SYMBOL", "NSE:NIFTY"))

    # --- timeframes ---
    timeframes: list[str] = field(default_factory=lambda: _env_list("TIMEFRAMES", ["1m", "5m"]))

    # --- scanning cadence ---
    scan_interval_sec: int = field(
        default_factory=lambda: _env_int("SCAN_INTERVAL_SEC", 20, lo=5, hi=3600))
    market_hours_only: bool = field(default_factory=lambda: _env_bool("MARKET_HOURS_ONLY", True))
    session_start: str = field(default_factory=lambda: _env_time("SESSION_START", "09:15"))
    session_end: str = field(default_factory=lambda: _env_time("SESSION_END", "15:30"))
    tz: str = field(default_factory=_env_tz)

    # --- lookback mode (GitHub Actions / cloud) ---
    # Each Actions run is a fresh machine: scan the last N minutes of closed
    # bars every run instead of waiting for new bars. Dedupe still comes from
    # the cached data/sent_alerts.json, so no alert is ever repeated.
    # 0 = disabled (live incremental mode).
    lookback_minutes: int = field(
        default_factory=lambda: _env_int("LOOKBACK_MINUTES", 0, lo=0, hi=1440))

    # --- telegram (both bots receive the SAME alerts) ---
    telegram_enabled: bool = field(default_factory=lambda: _env_bool("TELEGRAM_ENABLED", True))
    bot1_token: str = field(default_factory=lambda: _env_str("BOT1_TOKEN", ""))
    bot2_token: str = field(default_factory=lambda: _env_str("BOT2_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: _env_str("CHAT_ID", ""))
    chat_id_2: str = field(
        default_factory=lambda: _env_str("CHAT_ID_2", "") or _env_str("CHAT_ID", ""))

    # --- alert options ---
    sweep_alerts: bool = field(default_factory=lambda: _env_bool("SWEEP_ALERTS", True))
    # Never send signals for closed bars older than this many minutes
    # (protects against a restart with an old state file / long data outage).
    # Applies to live incremental mode only — the lookback window bounds
    # itself. 0 disables the guard.
    max_alert_age_min: int = field(
        default_factory=lambda: _env_int("MAX_ALERT_AGE_MIN", 10, lo=0, hi=1440))
    # Failed Telegram deliveries are retried for up to this long, then dropped.
    pending_max_age_min: int = field(
        default_factory=lambda: _env_int("PENDING_MAX_AGE_MIN", 30, lo=1, hi=1440))

    # --- webhook receiver (Mode 1: zero-delay TradingView alerts) ---
    webhook_host: str = field(default_factory=lambda: _env_str("WEBHOOK_HOST", "0.0.0.0"))
    webhook_port: int = field(
        default_factory=lambda: _env_int("WEBHOOK_PORT", 5000, lo=1, hi=65535))
    webhook_secret: str = field(default_factory=lambda: _env_str("WEBHOOK_SECRET", ""))

    # --- state / logging ---
    state_file: str = field(
        default_factory=lambda: _resolve(_env_str("STATE_FILE", "data/sent_alerts.json")))
    log_file: str = field(
        default_factory=lambda: _resolve(_env_str("LOG_FILE", "logs/scanner.log")))
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO").upper())

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        # Drop timeframes the feed cannot serve rather than failing mid-session.
        good = [tf for tf in self.timeframes if tf in SUPPORTED_TIMEFRAMES]
        bad = [tf for tf in self.timeframes if tf not in SUPPORTED_TIMEFRAMES]
        if bad:
            log.warning("Ignoring unsupported timeframe(s) %s — supported: %s",
                        ", ".join(bad), ", ".join(SUPPORTED_TIMEFRAMES))
        self.timeframes = good or ["5m"]

        if self.session_start >= self.session_end:
            log.warning("SESSION_START (%s) is not before SESSION_END (%s) — "
                        "resetting to 09:15-15:30", self.session_start, self.session_end)
            self.session_start, self.session_end = "09:15", "15:30"

        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            log.warning("LOG_LEVEL=%r invalid — using INFO", self.log_level)
            self.log_level = "INFO"

    # ------------------------------------------------------------------
    def validate(self) -> list[str]:
        """Return human-readable warnings about a risky live configuration."""
        problems: list[str] = []
        if self.telegram_enabled:
            if not self.bot1_token and not self.bot2_token:
                problems.append("No BOT1_TOKEN/BOT2_TOKEN set — alerts will only be logged.")
            if not self.chat_id and not self.chat_id_2:
                problems.append("No CHAT_ID set — alerts will only be logged.")
            if self.bot1_token and self.bot2_token and self.bot1_token == self.bot2_token \
                    and self.chat_id == self.chat_id_2:
                problems.append("BOT1 and BOT2 are the same bot AND chat — "
                                "you will receive every alert twice.")
        if self.tz != "Asia/Kolkata":
            problems.append(
                f"Exchange timezone is {self.tz}, not Asia/Kolkata — the NSE session "
                f"window {self.session_start}-{self.session_end} will be wrong. "
                f"Set SCANNER_TZ=Asia/Kolkata (SCANNER_TZ overrides a container's TZ).")
        fastest = min((tf for tf in self.timeframes), key=_tf_seconds, default="5m")
        if self.scan_interval_sec > _tf_seconds(fastest):
            problems.append(
                f"SCAN_INTERVAL_SEC={self.scan_interval_sec}s is longer than the "
                f"{fastest} bar — closed bars can be detected late.")
        return problems


def _tf_seconds(tf: str) -> int:
    units = {"m": 60, "h": 3600, "d": 86400}
    try:
        return int(tf[:-1]) * units[tf[-1]]
    except (ValueError, KeyError, IndexError):
        return 300
