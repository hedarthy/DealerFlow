"""Eastern-time helper (stdlib only) for the on-demand ticker alert.

Vendored, hermetic copy of just the ``eastern_now()`` clock. Every time-sensitive
decision in the exposure math (notably time-to-expiry, which drives near-expiry gamma)
must be measured in US/Eastern wall-clock, not UTC, so a UTC host can't understate
time-to-close and distort 0-2 DTE greeks.

This is on-demand, so the NYSE trading-day calendar and schedule-slot helpers the hourly
SPY alert needs are deliberately omitted — only the Eastern clock is kept.

Pure stdlib (datetime + zoneinfo); safe under system python3 with no third-party deps.
"""
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - missing tzdata; degrade to naive local time
    _ET = None


def eastern_now():
    """Current US/Eastern wall-clock as a *naive* datetime.

    Naive (tz-stripped) so it composes with the naive expiry datetimes used across the
    pipeline without tz-aware/naive comparison errors. Falls back to naive local time
    only if the tz database is unavailable.
    """
    if _ET is None:
        return datetime.now()
    return datetime.now(_ET).replace(tzinfo=None)
