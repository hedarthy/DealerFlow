"""Offline unit tests for the analytical overlays (no network, no heavy deps).

Run with:  python tests/test_analysis.py
Covers the GEX dealer-positioning directional overlay, the EMA price-action
overlay, and the Eastern-time helper that fixes UTC-runner DTE/greek drift.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scorer import gex_directional_adjustment as gd, price_action_adjustment as pa
from src.gex_calculator import cumulative_zero_cross as flip, select_window_strikes
from src.timeutil import eastern_now

# Positive-gamma book used across the directional cases.
FLIP, CW, PW, EM = 98.0, 105.0, 92.0, 2.0


def _pts(*a, **k):
    return gd(*a, **k)[0]


def test_gex_directional():
    # Call, above flip with real room to the call wall -> confirm.
    assert _pts("call", 100.0, FLIP, CW, PW, "positive", em_pct=EM) > 0
    # Call already past the call wall -> upside capped -> oppose.
    assert _pts("call", 106.0, FLIP, CW, PW, "positive", em_pct=EM) < 0
    # Call sitting AT the call wall (inside wall-room band) -> neutral, not confirm.
    assert _pts("call", 104.5, FLIP, CW, PW, "positive", em_pct=EM) == 0.0
    # Call clearly below the flip -> lost support -> oppose.
    assert _pts("call", 96.0, FLIP, CW, PW, "positive", em_pct=EM) < 0
    # Call just below the flip (reclaim zone, inside band) -> neutral, NOT oppose.
    assert _pts("call", 97.6, FLIP, CW, PW, "positive", em_pct=EM) == 0.0

    # Put, below flip with room down to the put wall -> confirm.
    assert _pts("put", 95.0, FLIP, CW, PW, "positive", em_pct=EM) > 0
    # Put already past the put wall -> downside capped -> oppose.
    assert _pts("put", 91.0, FLIP, CW, PW, "positive", em_pct=EM) < 0
    # Put above the flip -> fighting support -> oppose.
    assert _pts("put", 100.0, FLIP, CW, PW, "positive", em_pct=EM) < 0

    # Negative-gamma book -> abstain (defer to momentum), regardless of side/location.
    assert _pts("call", 100.0, FLIP, CW, PW, "negative", em_pct=EM) == 0.0
    assert _pts("put", 95.0, FLIP, CW, PW, "negative", em_pct=EM) == 0.0
    # No structure (flip unknown) -> neutral.
    assert _pts("call", 100.0, 0.0, CW, PW, "positive", em_pct=EM) == 0.0
    print("ok  gex_directional_adjustment")


def test_price_action():
    # Bull stack: confirms calls, opposes puts.
    assert pa("call", 105.0, 102.0, 100.0)[0] > 0
    assert pa("put", 105.0, 102.0, 100.0)[0] < 0
    # Bear stack: confirms puts, opposes calls.
    assert pa("put", 95.0, 98.0, 100.0)[0] > 0
    assert pa("call", 95.0, 98.0, 100.0)[0] < 0
    # Price tangled in the EMAs -> no opinion.
    assert pa("call", 101.0, 100.0, 102.0)[0] == 0.0
    # Missing EMAs -> abstain.
    assert pa("call", 100.0, None, None)[0] == 0.0
    print("ok  price_action_adjustment")


def test_cumulative_zero_cross():
    # Clean book: negative (put-side) low, positive (call-side) high -> flip near spot,
    # at the bracketing strike nearest spot, NOT a deep wing.
    assert flip({95: -40, 100: -10, 105: 30, 110: 25}, spot=100) == 105.0

    # Wing-wiggle suppression (the bug this fix targets): tiny mixed-sign exposure in
    # the deep wing (80/82) creates an early low->high zero-crossing that the OLD code
    # returned. The real near-money transition is at 95; the flip must land there, never
    # on the 82 wing blip.
    g = {80: -2, 82: 3, 95: -50, 105: 60}
    assert flip(g, spot=99) == 95.0
    assert flip(g, spot=99) != 82.0

    # Below-window mass is carried as a base so a large out-of-range put chunk still
    # pulls the cumulative negative and produces a genuine near-money crossing at 110.
    assert flip({50: -50, 110: 30, 120: 40}, spot=100) == 110.0

    # Genuinely one-sided (all positive) near-money book -> no flip -> 0.0 sentinel so
    # callers treat the structure as neutral instead of inventing a far level.
    assert flip({95: 10, 100: 20, 105: 30}, spot=100) == 0.0

    # Empty grid -> 0.0.
    assert flip({}, spot=100) == 0.0

    # No spot supplied -> fall back to the first significant crossing scanning upward.
    assert flip({95: -50, 105: 60}) == 105.0

    # Distance tie around spot -> break toward the endpoint whose cumulative is nearer
    # zero (the true crossing), not the lower strike. Here cum is -100 at 95 and +1 at
    # 105 (equidistant from spot 100); the flip belongs at 105.
    assert flip({95: -100, 105: 101}, spot=100) == 105.0

    # A huge far-OTM strike OUTSIDE the ±25% window must not inflate the threshold and
    # suppress a genuine near-money flip (threshold is scaled to windowed mass only).
    assert flip({100: -3, 105: 4, 300: 1000}, spot=100) == 100.0

    # Spot set but no strike inside the window -> neutral 0.0, never a far invented flip.
    assert flip({200: 5, 210: -5}, spot=100) == 0.0
    print("ok  cumulative_zero_cross")


def test_select_window_strikes():
    strikes = list(range(90, 121))  # 90..120 inclusive, $1 apart

    # 3 up / 3 down around spot 100: strike == spot groups with the at/below side.
    assert select_window_strikes(strikes, 100.0, n=3) == [98.0, 99.0, 100.0, 101.0, 102.0, 103.0]
    # Result is ascending and centered on spot.
    w = select_window_strikes(strikes, 105.4, n=5)
    assert w == sorted(w)
    assert max(k for k in w if k <= 105.4) == 105.0
    assert min(k for k in w if k > 105.4) == 106.0

    # Fewer than n available below spot -> return what exists, no padding/error.
    assert select_window_strikes(strikes, 91.0, n=5) == [90.0, 91.0, 92.0, 93.0, 94.0, 95.0, 96.0]

    # Exactly n per side caps the window at 2*n and drops the far wings.
    assert len(select_window_strikes(strikes, 105.0, n=2)) == 4
    assert select_window_strikes(strikes, 105.0, n=2) == [104.0, 105.0, 106.0, 107.0]

    # Dedupes and ignores Nones; empty / invalid spot degrade gracefully.
    assert select_window_strikes([100, 100, None, 101], 100.0, n=5) == [100.0, 101.0]
    assert select_window_strikes([], 100.0) == []
    assert select_window_strikes(strikes, 0, n=2) == [90.0, 91.0, 92.0, 93.0]  # no spot -> first 2n
    print("ok  select_window_strikes")


def test_eastern_now():
    now = eastern_now()
    assert isinstance(now, datetime)
    assert now.tzinfo is None  # naive, so it composes with naive expiry datetimes
    print("ok  eastern_now")


if __name__ == "__main__":
    test_gex_directional()
    test_price_action()
    test_cumulative_zero_cross()
    test_select_window_strikes()
    test_eastern_now()
    print("\nAll analysis tests passed.")
