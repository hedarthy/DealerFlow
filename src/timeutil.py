"""Market-time helpers.

Scheduled CI runners are UTC, but every time-sensitive decision in this
screener — time-to-expiry (and therefore near-expiry gamma), the DTE window,
the 0-DTE "already past today's close" cutoff, and the report date — must be
measured in US market wall-clock (US/Eastern), not UTC. Reading the clock in
UTC understates time-to-close by 4-5 hours and badly distorts 0-2 DTE greeks,
which is exactly the part of the chain this tool trades.
"""
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - missing tzdata; degrade to naive local time
    _ET = None


def eastern_now():
    """Current US/Eastern wall-clock as a *naive* datetime.

    Naive (tz-stripped) so it composes with the naive expiry datetimes used
    across the pipeline without tz-aware/naive comparison errors. Falls back to
    naive local time only if the tz database is unavailable.
    """
    if _ET is None:
        return datetime.now()
    return datetime.now(_ET).replace(tzinfo=None)
