"""Offline tests for the NYSE trading-day calendar and the schedule gate.

Run with:  python tests/test_schedule.py
No network, stdlib only. Verifies the holiday calendar against known NYSE
closures (including weekend-observance edge cases) and that the dual EST/EDT
crons collapse to exactly one intended run per day, year-round.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.market_calendar import (
    is_trading_day, nyse_holidays, cron_scheduled_et_hour, INTENDED_ET_HOUR,
)

# Known NYSE full-closure holidays (verified against the published calendars),
# chosen to cover weekend-observance edge cases.
KNOWN_HOLIDAYS = {
    2025: [
        date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
        date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
        date(2025, 11, 27), date(2025, 12, 25),
    ],
    2026: [
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3),   # Jul 4 (Sat) -> Fri Jul 3
        date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    ],
    2027: [
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18),   # Jun 19 (Sat) -> Fri Jun 18
        date(2027, 7, 5),                        # Jul 4 (Sun) -> Mon Jul 5
        date(2027, 9, 6), date(2027, 11, 25),
        date(2027, 12, 24),                      # Dec 25 (Sat) -> Fri Dec 24
    ],
}


def test_known_holidays():
    for year, days in KNOWN_HOLIDAYS.items():
        computed = nyse_holidays(year)
        assert computed == set(days), (
            f"{year}: missing={set(days) - computed} extra={computed - set(days)}")
        for d in days:
            assert not is_trading_day(d), f"{d} should be a market holiday"
    print("ok  known NYSE holidays (2025-2027, incl. observance shifts)")


def test_trading_and_nontrading_days():
    # Regular weekday sessions.
    assert is_trading_day(date(2026, 6, 5))    # Friday
    assert is_trading_day(date(2026, 6, 8))    # Monday
    # Weekends are never trading days.
    assert not is_trading_day(date(2026, 6, 6))   # Saturday
    assert not is_trading_day(date(2026, 6, 7))   # Sunday
    # New Year's Day on a Saturday is NOT observed on the prior Friday.
    assert date(2028, 1, 1).weekday() == 5
    assert date(2027, 12, 31) not in nyse_holidays(2027)
    assert is_trading_day(date(2027, 12, 31))     # stays a trading day
    print("ok  trading / non-trading day classification")


def _gate_open(mode, cron_str, on_date):
    """Mirror the agent gate: it proceeds iff today is a trading day AND this
    cron's SCHEDULED ET hour equals the intended hour (independent of how late
    the runner actually starts)."""
    if not is_trading_day(on_date):
        return False
    return cron_scheduled_et_hour(cron_str, on_date) == INTENDED_ET_HOUR[mode]


# The actual cron strings shipped in the workflows.
CRONS = {
    "morning": ["25 13 * * 1-5", "25 14 * * 1-5"],
    "close": ["0 20 * * 1-5", "0 21 * * 1-5"],
}


def test_dual_cron_collapses_to_one_run():
    # A summer (EDT) and a winter (EST) trading weekday.
    for day in (date(2026, 7, 15), date(2026, 1, 15)):
        assert is_trading_day(day)
        for mode, crons in CRONS.items():
            opens = [_gate_open(mode, c, day) for c in crons]
            assert sum(opens) == 1, f"{mode} on {day}: expected exactly 1 run, got {opens}"
    # On a holiday, neither cron proceeds (e.g. Thanksgiving 2026-11-26).
    for mode, crons in CRONS.items():
        for c in crons:
            assert not _gate_open(mode, c, date(2026, 11, 26))
    print("ok  dual cron collapses to exactly one intended run/day (and skips holidays)")


def test_latency_does_not_double_post_or_miss():
    # The gate keys off the cron's SCHEDULED hour, so scheduler latency on the
    # actual start time cannot change the decision: in winter the early "EDT"
    # cron (scheduled 08:25 / 15:00 ET) self-skips even if delayed into the
    # intended hour, and the correct cron still owns the run even if it starts
    # late. Verify exactly one cron is eligible per season regardless of delay.
    winter = date(2026, 1, 15)
    # Morning: EDT cron scheduled 08 ET (skip), EST cron scheduled 09 ET (run).
    assert cron_scheduled_et_hour("25 13 * * 1-5", winter) == 8
    assert cron_scheduled_et_hour("25 14 * * 1-5", winter) == 9
    assert not _gate_open("morning", "25 13 * * 1-5", winter)   # delayed off-season -> still skip
    assert _gate_open("morning", "25 14 * * 1-5", winter)       # correct cron -> run
    # Close: EDT cron scheduled 15 ET (skip), EST cron scheduled 16 ET (run).
    assert cron_scheduled_et_hour("0 20 * * 1-5", winter) == 15
    assert cron_scheduled_et_hour("0 21 * * 1-5", winter) == 16
    summer = date(2026, 7, 15)
    # Summer flips which cron is correct.
    assert cron_scheduled_et_hour("25 13 * * 1-5", summer) == 9
    assert cron_scheduled_et_hour("25 14 * * 1-5", summer) == 10
    print("ok  latency cannot cause a double-post or a silent miss")


if __name__ == "__main__":
    test_known_holidays()
    test_trading_and_nontrading_days()
    test_dual_cron_collapses_to_one_run()
    test_latency_does_not_double_post_or_miss()
    print("\nAll schedule tests passed.")
