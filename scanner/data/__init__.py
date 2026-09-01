"""Market-data feeds for the BSL/SSL scanner."""

from scanner.data.yfinance_feed import INTERVAL_DELTA, YFinanceFeed  # noqa: F401

__all__ = ["YFinanceFeed", "INTERVAL_DELTA"]
