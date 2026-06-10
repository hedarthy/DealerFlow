"""NYSE trading-day calendar (stdlib only).

Scheduled CI runs fire on weekdays via cron, but cron has no concept of US
market holidays. Posting a "morning"/"close" alert on Thanksgiving or July 4th
is noise at best and misleading at worst (stale chains, no real session). This
module answers one question — "is ``d`` a regular NYSE trading day?" — using the
standard observed-holiday rules so the agent can self-skip non-session days.

Pure stdlib (datetime only); safe to import under system python3 with no deps.
Half-day early closes (e.g. day after Thanksgiving) are still trading days and
are intentionally treated as normal here.
"""
from datetime import date, datetime, timedelta

# Each scheduled mode is gated to a single intended ET hour so that the dual
# (EST + EDT) UTC crons collapse to exactly one real run per day, year-round.
INTENDED_ET_HOUR = {"morning": 9, "close": 16}

# The full intended ET wall-clock (hour, minute) each mode targets. The morning
# read is a pre-open/at-open momentum signal (09:25 ET, just before the 09:30
# open); the close read snapshots the 16:00 ET closing chain. Used to measure how
# late GitHub's best-effort scheduler actually started the run.
INTENDED_ET_TIME = {"morning": (9, 25), "close": (16, 0)}

# How many minutes past the intended ET time a run may still post before its
# signal is considered stale and dropped. The morning window is tight because the
# whole point is a fresh open read — by the time the tape has moved an hour the
# entry/levels are wrong (a 3-hour-late post is exactly the bug this guards). The
# close window is wider: it snapshots the closing chain, which doesn't go stale
# the moment the bell rings, and no later run will cover it.
FRESHNESS_MIN = {"morning": 30, "close": 45}


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


def cron_scheduled_et_hour(cron_str: str, on_date: date):
    """ET hour at which a UTC ``'M H * * ...'`` cron is *scheduled* to fire.

    Gating on the cron's scheduled time (rather than the runner's actual start
    time) is what makes the dual EST/EDT crons robust to GitHub's best-effort
    scheduler: only the cron that is correct for the current DST offset ever
    matches the intended ET hour, so a delayed off-season cron can never sneak
    through (no double-post) and a delayed correct cron is still recognized as
    the one that owns today's run (no silent miss). Returns the ET hour as an
    int, or ``None`` if the cron string or tz database can't be resolved.
    """
    try:
        from zoneinfo import ZoneInfo
        parts = cron_str.split()
        minute, hour = int(parts[0]), int(parts[1])
        utc_dt = datetime(on_date.year, on_date.month, on_date.day,
                          hour, minute, tzinfo=ZoneInfo("UTC"))
        return utc_dt.astimezone(ZoneInfo("America/New_York")).hour
    except Exception:
        return None


def minutes_late(mode: str, now_et: datetime) -> float:
    """Minutes ``now_et`` is past ``mode``'s intended ET time on the same date.

    Negative means the run started early (before the intended time). Used to drop
    a run that GitHub's scheduler fired so late the signal is already stale.
    """
    ih, im = INTENDED_ET_TIME[mode]
    intended = now_et.replace(hour=ih, minute=im, second=0, microsecond=0)
    return (now_et - intended).total_seconds() / 60.0
