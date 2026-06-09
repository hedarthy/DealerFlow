"""Offline tests for the screener extras added alongside the SPY-visual port:

  * the VEX per-1.00σ rescale (and its selection invariance),
  * the spot-centred strike window helper (with a forced pick strike),
  * the event-day filter (manual, pure-stdlib path), and
  * a heatmap-triptych render smoke test (skipped if matplotlib is unavailable).

Run with:  python tests/test_screener_extras.py
The first three groups need no heavy deps; the heatmap smoke runs only when
numpy/pandas/matplotlib/seaborn are importable (i.e. under the project venv).
"""
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gex_calculator import bs_greeks, contract_exposures, select_window_strikes
from src.scorer import (
    score_components, weighted_score, dominant_edge,
    vanna_directional_adjustment, flow_imbalance_adjustment, aggregate_conviction,
)
from src.event_calendar import (
    _to_date, ticker_event_dates, event_in_window,
)

WEIGHTS = {
    "gex_regime": 0.20, "flow_proxy": 0.25, "squeeze": 0.15,
    "vanna_charm": 0.25, "moneyness_dte": 0.15,
}


def test_vex_per_one_sigma():
    # VEX must now be vanna * notional * spot (per 1.00σ), i.e. 100x the old
    # per-0.01-vol-point figure — matching the SPY pipeline + vendor convention.
    S, K, T, iv, oi, sign = 100.0, 105.0, 0.02, 0.30, 1200, 1
    gamma, vanna, charm = bs_greeks(S, K, T, 0.05, iv)
    gex, vex, cex = contract_exposures(S, K, T, iv, oi, sign)
    notional = oi * 100 * sign
    assert abs(vex - vanna * notional * S) < 1e-6
    # Sanity: it is exactly 100x the old scaling and shares vanna's sign.
    assert abs(vex - 100.0 * (vanna * notional * S * 0.01)) < 1e-6
    assert (vex > 0) == (vanna > 0)
    print("ok  VEX scaled per 1.00 sigma (vanna * notional * spot)")


def test_vex_rescale_is_selection_invariant():
    # A uniform positive rescale of VEX (strike + chain max together) must leave the
    # score, its components and the dominant edge unchanged — only DISPLAY changes.
    row = {"openInterest": 2000, "volume": 6000, "strike": 102.0}
    kw = dict(spot=100.0, dte=1, gex_balance=0.4, em_pct=2.0, charm_ex=5e5, max_cex=1e6)
    base = score_components(row, vanna_ex=8e5, max_vex=1e6, **kw)
    scaled = score_components(row, vanna_ex=8e7, max_vex=1e8, **kw)  # x100 VEX
    for k in base:
        assert abs(base[k] - scaled[k]) < 1e-9, k
    assert abs(weighted_score(base, WEIGHTS) - weighted_score(scaled, WEIGHTS)) < 1e-9
    assert dominant_edge(base, WEIGHTS) == dominant_edge(scaled, WEIGHTS)
    print("ok  VEX rescale leaves score/components/edge invariant")


def test_select_window_strikes():
    strikes = [90 + i for i in range(21)]  # 90..110
    win = select_window_strikes(strikes, spot=100.0, n=3)
    assert win == [98.0, 99.0, 100.0, 101.0, 102.0, 103.0]  # 3 below (incl spot) + 3 above
    # A strike exactly at spot groups with the at/below side.
    assert 100.0 in win and 101.0 in win
    # must_include forces an out-of-window strike in, sorted into place, marked-able.
    win2 = select_window_strikes(strikes, spot=100.0, n=3, must_include=108.0)
    assert 108.0 in win2 and win2 == sorted(win2)
    # No spot -> just the first 2n strikes; empty + must_include -> [strike].
    assert select_window_strikes(strikes, spot=0, n=2) == [90.0, 91.0, 92.0, 93.0]
    assert select_window_strikes([], spot=100.0, must_include=80.0) == [80.0]
    print("ok  select_window_strikes (centered, spot-grouped, must_include forced)")


def test_to_date_coercion():
    assert _to_date("2026-06-08") == date(2026, 6, 8)
    assert _to_date("2026-06-08T13:30:00") == date(2026, 6, 8)
    assert _to_date(datetime(2026, 6, 8, 9, 30)) == date(2026, 6, 8)
    assert _to_date(date(2026, 6, 8)) == date(2026, 6, 8)
    for junk in (None, "", "NaT", "nan", "not-a-date", "2026-13-40"):
        assert _to_date(junk) is None, junk
    print("ok  _to_date coercion (ISO / datetime / date, NaT/None/garbage -> None)")


def test_event_filter_manual():
    manual = {"AAPL": ["2026-06-08"]}  # WWDC keynote
    events = ticker_event_dates("AAPL", manual, enable_earnings=False)
    assert events == [(date(2026, 6, 8), "event")]
    # 0-2 DTE run INTO the event -> flagged.
    hit, lbl = event_in_window(events, date(2026, 6, 5), date(2026, 6, 9))
    assert hit and lbl == "event"
    # Same-day expiry on the event date -> flagged (inclusive, by design: event
    # time-of-day isn't reliable, so we conservatively treat the whole date as risky).
    assert event_in_window(events, date(2026, 6, 8), date(2026, 6, 8))[0]
    # Horizon entirely before, or entirely after, the event date -> NOT flagged.
    assert event_in_window(events, date(2026, 6, 1), date(2026, 6, 7)) == (False, None)
    assert event_in_window(events, date(2026, 6, 9), date(2026, 6, 10)) == (False, None)
    # A ticker with no configured/earnings events is never flagged.
    assert ticker_event_dates("ZZZZ", manual, enable_earnings=False) == []
    # Reversed window bounds are tolerated.
    assert event_in_window(events, date(2026, 6, 9), date(2026, 6, 5))[0]
    print("ok  event filter (manual path: per-window inclusive, pre/post-event clear)")


def test_heatmap_triptych_smoke():
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
        import matplotlib  # noqa: F401
        import seaborn  # noqa: F401
        from src.heatmap import render_pick_triptych
    except Exception as e:
        print(f"skip heatmap triptych smoke (deps unavailable: {e})")
        return
    import tempfile
    # Build small per-strike grids around spot 100; pick a strike (108) that sits
    # OUTSIDE the n=25 default window only if sparse — here it's inside, but we still
    # confirm the render succeeds and writes a non-trivial PNG.
    strikes = [float(s) for s in range(90, 111)]
    gex = {k: (k - 100.0) * 1e5 for k in strikes}
    vex = {k: (100.0 - k) * 8e4 for k in strikes}
    cex = {k: (k - 99.0) * 3e4 for k in strikes}
    contract = {"ticker": "TEST", "strike": 104.0, "type": "call",
                "exp": "2026-06-19", "spot": 100.0}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "tri.png")
        render_pick_triptych(contract, gex, vex, cex, path)
        assert os.path.exists(path) and os.path.getsize(path) > 5000
        # A pick strike outside the centred window must still render (forced in).
        sparse = {k: gex[k] for k in strikes}
        sparse[300.0] = 9e5
        v2 = dict(vex); v2[300.0] = -2e5
        x2 = dict(cex); x2[300.0] = 1e5
        c2 = dict(contract, strike=300.0)
        path2 = os.path.join(d, "tri2.png")
        render_pick_triptych(c2, sparse, v2, x2, path2)
        assert os.path.exists(path2) and os.path.getsize(path2) > 5000
    print("ok  render_pick_triptych smoke (GEX/VEX/CEX, pick strike marked + forced)")


def test_candidates_table_smoke():
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
        import matplotlib  # noqa: F401
        from src.heatmap import render_candidates_table
    except Exception as e:
        print(f"skip candidates table smoke (deps unavailable: {e})")
        return
    import tempfile
    lower_conv = [
        ({"ticker": "META", "type": "put", "strike": 610.0, "otm": 0.4, "dte": 0,
          "score": 73.2, "vol_oi": 3.7, "edge": "vanna"},),
        ({"ticker": "NVDA", "type": "call", "strike": 210.0, "otm": 0.6, "dte": 0,
          "score": 66.6, "vol_oi": 12.4, "edge": "flow"},),
        # An event-risk row must render its gold "<label>-risk" edge.
        ({"ticker": "AAPL", "type": "call", "strike": 205.0, "otm": 1.1, "dte": 2,
          "score": 80.0, "vol_oi": 9.0, "edge": "flow",
          "event_risk": True, "event_label": "WWDC"},),
    ]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "candidates.png")
        render_candidates_table(lower_conv, "morning", "2026-06-05", path)
        assert os.path.exists(path) and os.path.getsize(path) > 5000
        # Single-row + empty-ish edge still render without error.
        one = [({"ticker": "SPY", "type": "call", "strike": 750.0, "otm": 0.5,
                 "dte": 0, "score": 61.9, "vol_oi": 43.8, "edge": ""},)]
        p2 = os.path.join(d, "one.png")
        render_candidates_table(one, "close", "2026-06-05", p2)
        assert os.path.exists(p2) and os.path.getsize(p2) > 5000
    print("ok  render_candidates_table smoke (calls/puts, event-risk row, 1-row)")


def test_vanna_directional_adjustment():
    # Positive net dealer vanna (positive-γ, IV-drop -> dealer buying) confirms calls, opposes puts.
    assert vanna_directional_adjustment("call", net_vex=1e6, gross_vex=1e6) == (8.0, "vanna tailwind (+net vanna, IV↓) ✓")
    p, _ = vanna_directional_adjustment("put", net_vex=1e6, gross_vex=1e6)
    assert p == -8.0
    # Negative net vanna flips the alignment.
    assert vanna_directional_adjustment("put", net_vex=-1e6, gross_vex=1e6)[0] == 8.0
    assert vanna_directional_adjustment("call", net_vex=-1e6, gross_vex=1e6)[0] == -8.0
    # Strength scales with |net|/gross.
    assert abs(vanna_directional_adjustment("call", net_vex=0.5, gross_vex=1.0)[0] - 4.0) < 1e-9
    # A balanced book (|bal| < min_frac) and an empty book are neutral.
    assert vanna_directional_adjustment("call", net_vex=0.02, gross_vex=1.0) == (0.0, "vanna balanced")
    assert vanna_directional_adjustment("call", net_vex=5.0, gross_vex=0.0) == (0.0, "vanna n/a")
    # In a NEGATIVE-gamma book the IV-direction assumption breaks (spot-vol correlation),
    # so the overlay abstains regardless of the vanna sign.
    assert vanna_directional_adjustment("put", net_vex=-1e6, gross_vex=1e6, regime="negative") == (0.0, "vanna n/a (neg-γ: IV dir ambiguous)")
    assert vanna_directional_adjustment("call", net_vex=1e6, gross_vex=1e6, regime="negative")[0] == 0.0
    print("ok  vanna_directional_adjustment (regime-gated sign, strength, neutral cases)")


def test_flow_imbalance_adjustment():
    # Call-heavy traded premium confirms calls, opposes puts; strength scales with skew.
    pts, lbl = flow_imbalance_adjustment("call", call_prem=300.0, put_prem=100.0)
    assert abs(pts - 4.0) < 1e-9 and "calls-led" in lbl and "✓" in lbl
    assert abs(flow_imbalance_adjustment("put", 300.0, 100.0)[0] + 4.0) < 1e-9
    # Put-heavy flips it.
    assert flow_imbalance_adjustment("put", 100.0, 300.0)[0] > 0
    assert flow_imbalance_adjustment("call", 100.0, 300.0)[0] < 0
    # A two-sided tape (|imb| < min_frac) and no premium are neutral.
    assert flow_imbalance_adjustment("call", 105.0, 95.0) == (0.0, "flow two-sided")
    assert flow_imbalance_adjustment("call", 0.0, 0.0) == (0.0, "flow n/a")
    print("ok  flow_imbalance_adjustment (sign, strength, two-sided/n-a neutral)")


def test_aggregate_conviction():
    # Positive-gamma weighting leans on structure (gex 1.2) + flow, down-weights EMA (0.7).
    conv, aligned, opposed = aggregate_conviction(12.0, 10.0, 8.0, 8.0, "positive")
    assert abs(conv - (0.7 * 12 + 1.2 * 10 + 1.0 * 8 + 0.9 * 8)) < 1e-9
    assert (aligned, opposed) == (4, 0)
    # Negative-gamma weighting up-weights EMA (1.2) + flow (1.1), cuts structure (0.5).
    conv_n, _, _ = aggregate_conviction(12.0, 10.0, 8.0, 8.0, "negative")
    assert abs(conv_n - (1.2 * 12 + 0.8 * 10 + 1.0 * 8 + 1.1 * 8)) < 1e-9
    # adaptive=False is a plain unweighted sum.
    assert abs(aggregate_conviction(12.0, 10.0, 8.0, 8.0, "positive", adaptive=False)[0] - 38.0) < 1e-9
    # Alignment counts use the unweighted signs; an unknown regime falls back to negative.
    conv_m, al, op = aggregate_conviction(-15.0, 0.0, 8.0, -4.0, "neutral")
    assert (al, op) == (1, 2)
    print("ok  aggregate_conviction (regime weighting, counts, adaptive toggle, fallback)")


def test_flow_premium_split():
    try:
        import pandas as pd
        from datetime import timedelta
        from src.daily_options_agent import _flow_premium_split
        from src.timeutil import eastern_now
    except Exception as e:
        print(f"skip flow premium split (deps unavailable: {e})")
        return
    today = eastern_now().date()
    near = (today + timedelta(days=1)).strftime("%Y-%m-%d")   # dte 1, in window
    far = (today + timedelta(days=30)).strftime("%Y-%m-%d")   # dte 30, excluded
    near_df = pd.DataFrame([
        {"opt_type": "call", "volume": 100, "lastPrice": 2.0},   # 100*2*100 = 20,000
        {"opt_type": "put", "volume": 50, "lastPrice": 1.0},     # 50*1*100  =  5,000
    ])
    far_df = pd.DataFrame([{"opt_type": "call", "volume": 999, "lastPrice": 9.0}])
    call_p, put_p = _flow_premium_split({near: near_df, far: far_df}, dte_max=2)
    assert abs(call_p - 20000.0) < 1e-6 and abs(put_p - 5000.0) < 1e-6  # far expiry excluded
    print("ok  _flow_premium_split (call/put premium, DTE-window filtered)")


if __name__ == "__main__":
    test_vex_per_one_sigma()
    test_vex_rescale_is_selection_invariant()
    test_select_window_strikes()
    test_to_date_coercion()
    test_event_filter_manual()
    test_vanna_directional_adjustment()
    test_flow_imbalance_adjustment()
    test_aggregate_conviction()
    test_flow_premium_split()
    test_heatmap_triptych_smoke()
    test_candidates_table_smoke()
    print("\nAll screener-extras tests passed.")
