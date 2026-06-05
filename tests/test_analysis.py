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


def test_eastern_now():
    now = eastern_now()
    assert isinstance(now, datetime)
    assert now.tzinfo is None  # naive, so it composes with naive expiry datetimes
    print("ok  eastern_now")


if __name__ == "__main__":
    test_gex_directional()
    test_price_action()
    test_eastern_now()
    print("\nAll analysis tests passed.")
