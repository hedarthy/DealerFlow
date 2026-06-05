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
from src.gex_calculator import cumulative_zero_cross as flip
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


def test_eastern_now():
    now = eastern_now()
    assert isinstance(now, datetime)
    assert now.tzinfo is None  # naive, so it composes with naive expiry datetimes
    print("ok  eastern_now")


if __name__ == "__main__":
    test_gex_directional()
    test_price_action()
    test_cumulative_zero_cross()
    test_eastern_now()
    print("\nAll analysis tests passed.")
