"""NYSE trading-day calendar + Eastern-time helpers (stdlib only) for the SPY alert.

Vendored copy (timeutil + market_calendar) so this package is hermetic. Scheduled CI
runners are UTC, but every time-sensitive decision — time-to-expiry, the 0DTE cutoff,
the schedule slot gate — must be measured in US/Eastern wall-clock, not UTC.

Pure stdlib (datetime + zoneinfo); safe under system python3 with no third-party deps.
"""
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - missing tzdata; degrade to naive local time
    _ET = None

# Intended ET (hour, minute) slots for the hourly SPY dealerflow alert: one minute
# after the open, then on the hour through the close. The dual EST/EDT UTC crons in
# spy-gex-run.yml each map to exactly ONE ET time per date (DST-correct), and the
# agent runs only when that scheduled ET time lands on one of these slots — so the
# off-DST cron self-skips and there is no double-post (see cron_scheduled_et_time).
SPY_GEX_SLOTS = frozenset({
    (9, 31), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (16, 0),
})


def eastern_now():
    """Current US/Eastern wall-clock as a *naive* datetime.

    Naive (tz-stripped) so it composes with the naive expiry datetimes used across
    the pipeline without tz-aware/naive comparison errors. Falls back to naive local
    time only if the tz database is unavailable.
    """
    if _ET is None:
        return datetime.now()
    return datetime.now(_ET).replace(tzinfo=None)


def easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous/Meeus computus)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th ``weekday`` (Mon=0) of ``month`` (n is 1-based)."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` (Mon=0) of ``month``."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed_fixed(d: date) -> date:
    """NYSE observance for a fixed-date holiday: Sat->Fri, Sun->Mon."""
    if d.weekday() == 5:        # Saturday -> preceding Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:        # Sunday -> following Monday
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> set:
    """Set of observed full-closure NYSE holidays for ``year``."""
    hols = set()

    # New Year's Day. Special case: when Jan 1 is a Saturday the NYSE does NOT
    # observe it on Dec 31 (the prior Friday stays a trading day), so it simply
    # drops off the weekday calendar.
    nyd = date(year, 1, 1)
    if nyd.weekday() == 6:                      # Sunday -> Monday Jan 2
        hols.add(date(year, 1, 2))
    elif nyd.weekday() < 5:                     # Mon-Fri observed as-is
        hols.add(nyd)

    hols.add(_nth_weekday(year, 1, 0, 3))       # MLK Day, 3rd Mon Jan
    hols.add(_nth_weekday(year, 2, 0, 3))       # Washington's Birthday, 3rd Mon Feb
    hols.add(easter(year) - timedelta(days=2))  # Good Friday
    hols.add(_last_weekday(year, 5, 0))         # Memorial Day, last Mon May
    if year >= 2022:
        hols.add(_observed_fixed(date(year, 6, 19)))  # Juneteenth
    hols.add(_observed_fixed(date(year, 7, 4)))       # Independence Day
    hols.add(_nth_weekday(year, 9, 0, 1))       # Labor Day, 1st Mon Sep
    hols.add(_nth_weekday(year, 11, 3, 4))      # Thanksgiving, 4th Thu Nov
    hols.add(_observed_fixed(date(year, 12, 25)))     # Christmas
    return hols


def is_trading_day(d: date) -> bool:
    """True if ``d`` is a regular NYSE trading day (weekday, not a holiday)."""
    if d.weekday() >= 5:        # Saturday/Sunday
        return False
    return d not in nyse_holidays(d.year)


def early_close_dates(year: int) -> set:
    """NYSE half-day (1:00 PM ET early close) sessions for ``year``.

    Covers the three standard, rule-stable early closes: the Friday after
    Thanksgiving, Christmas Eve (Dec 24), and July 3 — but only when each is a real
    weekday trading day immediately adjacent to the corresponding holiday (so a
    weekend-shifted Dec 25 / July 4 doesn't spuriously mark a half-day, and a date
    that is itself the observed holiday is excluded). The hourly SPY alert uses this
    to suppress 2:00/3:00/4:00 PM slots that would otherwise fire after the 1:00 PM
    close on these days.
    """
    hols = nyse_holidays(year)
    out = set()
    out.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))  # Black Friday
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and date(year, 12, 25).weekday() < 5:
        out.add(dec24)                                          # Christmas Eve
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5 and date(year, 7, 4).weekday() < 5:
        out.add(jul3)                                          # July 3 (pre-July 4)
    return {d for d in out if d.weekday() < 5 and d not in hols}


def market_close_hm(d: date):
    """NYSE close time for ``d`` as an ET ``(hour, minute)`` tuple: ``(13, 0)`` on an
    early-close half day, otherwise the regular ``(16, 0)``."""
    return (13, 0) if d in early_close_dates(d.year) else (16, 0)


def cron_scheduled_et_time(cron_str: str, on_date: date):
    """ET ``(hour, minute)`` at which a UTC ``'M H * * ...'`` cron is *scheduled* to
    fire on ``on_date``.

    Converting the cron's scheduled UTC time to ET via the tz database makes the
    result DST-correct for ``on_date``, which is what lets the dual EST/EDT crons
    collapse to one real run: only the cron correct for the current offset maps onto
    an intended slot. Returns ``None`` if the cron string or tz database can't be
    resolved.
    """
    try:
        from zoneinfo import ZoneInfo
        parts = cron_str.split()
        minute, hour = int(parts[0]), int(parts[1])
        utc_dt = datetime(on_date.year, on_date.month, on_date.day,
                          hour, minute, tzinfo=ZoneInfo("UTC"))
        et = utc_dt.astimezone(ZoneInfo("America/New_York"))
        return et.hour, et.minute
    except Exception:
        return None
