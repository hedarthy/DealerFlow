"""Offline tests for the standalone SPY GEX alert (no network, stdlib + the package).

Run with:  python -m spy_gex.tests.test_spy_gex   (from the repo root)

Verifies the vendored math (strike window, gamma flip, dealer-signed exposure), the
Eastern-time helper, and the schedule gate (dual EST/EDT cron dedup, early-close
suppression, slot_decision). Importing nothing from ``src/`` keeps this package fully
self-contained.
"""
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from spy_gex.exposure import (
    cumulative_zero_cross as flip, select_window_strikes, compute_exposure_grids,
    get_key_levels, get_regime, bs_greeks, contract_exposures,
)
from spy_gex.calendar_util import (
    eastern_now, is_trading_day, cron_scheduled_et_time, market_close_hm,
    early_close_dates, SPY_GEX_SLOTS,
)
from spy_gex.agent import (
    slot_decision, build_greek_matrix, _annot_grid, render_grid, SKY_KING, _fmt_k,
    render_summary_table, build_summary_text, build_summary,
)

# Union of EST + EDT UTC crons declared in spy-gex-run.yml.
SPY_CRONS = [
    "31 13 * * 1-5", "31 14 * * 1-5",
    "0 14 * * 1-5", "0 15 * * 1-5", "0 16 * * 1-5", "0 17 * * 1-5",
    "0 18 * * 1-5", "0 19 * * 1-5", "0 20 * * 1-5", "0 21 * * 1-5",
]


def test_select_window_strikes():
    strikes = list(range(90, 121))  # 90..120 inclusive, $1 apart
    assert select_window_strikes(strikes, 100.0, n=3) == [98.0, 99.0, 100.0, 101.0, 102.0, 103.0]
    w = select_window_strikes(strikes, 105.4, n=5)
    assert w == sorted(w)
    assert max(k for k in w if k <= 105.4) == 105.0
    assert min(k for k in w if k > 105.4) == 106.0
    assert select_window_strikes(strikes, 91.0, n=5) == [90.0, 91.0, 92.0, 93.0, 94.0, 95.0, 96.0]
    assert len(select_window_strikes(strikes, 105.0, n=2)) == 4
    assert select_window_strikes(strikes, 105.0, n=2) == [104.0, 105.0, 106.0, 107.0]
    assert select_window_strikes([100, 100, None, 101], 100.0, n=5) == [100.0, 101.0]
    assert select_window_strikes([], 100.0) == []
    assert select_window_strikes(strikes, 0, n=2) == [90.0, 91.0, 92.0, 93.0]  # no spot -> first 2n
    print("ok  select_window_strikes")


def test_cumulative_zero_cross():
    assert flip({95: -40, 100: -10, 105: 30, 110: 25}, spot=100) == 105.0
    g = {80: -2, 82: 3, 95: -50, 105: 60}
    assert flip(g, spot=99) == 95.0
    assert flip({95: 10, 100: 20, 105: 30}, spot=100) == 0.0
    assert flip({}, spot=100) == 0.0
    print("ok  cumulative_zero_cross")


def test_exposure_dealer_sign():
    # A call-heavy book above spot is dealer long gamma there (positive GEX); a put-heavy
    # book below spot is dealer short gamma there (negative GEX). Regime follows the net.
    expiry = (eastern_now().date() + timedelta(days=30)).strftime("%Y-%m-%d")
    df = pd.DataFrame([
        {"strike": 105.0, "opt_type": "call", "openInterest": 5000,
         "impliedVolatility": 0.18, "contractSymbol": "SPY___C"},
        {"strike": 95.0, "opt_type": "put", "openInterest": 1000,
         "impliedVolatility": 0.20, "contractSymbol": "SPY___P"},
    ])
    gex, vex, cex = compute_exposure_grids(df, spot=100.0, expiry=expiry)
    assert gex[105.0] > 0
    assert gex[95.0] < 0
    assert get_regime(gex) == "positive"   # call leg (5x OI) dominates the net
    keys = get_key_levels(gex, spot=100.0)
    assert keys["call_wall"] == 105.0
    assert keys["put_wall"] == 95.0
    print("ok  exposure dealer sign / key levels")


def test_vex_per_full_sigma():
    # VEX is quoted per a FULL 1.00 sigma move in IV (one whole vol point), NOT per
    # 0.01 vol-point. So vex must equal vanna * (oi*100*sign) * spot with NO 0.01
    # factor -- i.e. 100x a per-vol-point figure. This pins the scaling convention.
    spot, strike, T, iv, oi, sign = 100.0, 105.0, 30 / 365.25, 0.18, 5000, 1
    _, vanna, _ = bs_greeks(spot, strike, T, 0.05, iv)
    _, vex, _ = contract_exposures(spot, strike, T, iv, oi, sign)
    expected = vanna * (oi * 100 * sign) * spot          # per 1.00 sigma
    assert abs(vex - expected) < 1e-9
    assert abs(vex - 100.0 * (expected * 0.01)) < 1e-9   # 100x the per-vol-point value
    print("ok  VEX scaled per 1.00 sigma (full vol point, no 0.01)")


def test_eastern_now():
    now = eastern_now()
    assert isinstance(now, datetime)
    assert now.tzinfo is None  # naive, composes with naive expiry datetimes
    print("ok  eastern_now")


def _spy_open_slots(on_date):
    """The ET slots the cron gate would fire on ``on_date``: each cron's DST-correct
    scheduled ET time, kept iff it's an intended slot at/before the close."""
    if not is_trading_day(on_date):
        return []
    close = market_close_hm(on_date)
    out = []
    for c in SPY_CRONS:
        hm = cron_scheduled_et_time(c, on_date)
        if hm and hm in SPY_GEX_SLOTS and hm <= close:
            out.append(hm)
    return out


def test_spy_hourly_slots_dedupe():
    # Every full session — summer (EDT), winter (EST), and the weekday adjacent to each
    # 2026 DST switch — must collapse the dual crons to EXACTLY the 8 intended slots.
    intended = sorted(SPY_GEX_SLOTS)
    full_sessions = [
        date(2026, 7, 15), date(2026, 1, 15), date(2026, 3, 6), date(2026, 3, 9),
        date(2026, 10, 30), date(2026, 11, 2),
    ]
    for day in full_sessions:
        assert is_trading_day(day)
        slots = _spy_open_slots(day)
        assert len(slots) == len(set(slots)), f"{day}: duplicate slot fired {slots}"
        assert sorted(slots) == intended, f"{day}: slots {sorted(slots)} != {intended}"
    print("ok  SPY dual crons collapse to the 8 intended slots (incl. DST-switch weeks)")


def test_spy_early_close_suppresses_afternoon():
    for half in (date(2025, 11, 28), date(2025, 12, 24), date(2025, 7, 3)):
        assert is_trading_day(half)
        assert market_close_hm(half) == (13, 0)
        slots = _spy_open_slots(half)
        assert (14, 0) not in slots and (15, 0) not in slots and (16, 0) not in slots
        assert sorted(slots) == [(9, 31), (10, 0), (11, 0), (12, 0), (13, 0)], slots
    assert market_close_hm(date(2026, 6, 5)) == (16, 0)
    assert len(_spy_open_slots(date(2026, 6, 5))) == 8
    print("ok  SPY early-close days suppress post-13:00 slots")


def test_early_close_calendar():
    assert date(2025, 11, 28) in early_close_dates(2025)   # Black Friday 2025
    assert date(2025, 12, 24) in early_close_dates(2025)   # Christmas Eve (Wed)
    assert date(2025, 7, 3) in early_close_dates(2025)     # July 3 (Thu, before Fri Jul 4)
    assert date(2026, 7, 3) not in early_close_dates(2026)  # weekend-shifted -> observed holiday
    assert not is_trading_day(date(2026, 7, 3))
    print("ok  early-close calendar (half days vs weekend-shifted holidays)")


def test_fmt_k_skylit_style():
    # $1,000.0K == $1,000,000; negatives use -$; zero is $0.0K.
    assert _fmt_k(1000.0) == "$1,000.0K"
    assert _fmt_k(-41686.1) == "-$41,686.1K"
    assert _fmt_k(0.0) == "$0.0K"
    assert _fmt_k(2458291.8) == "$2,458,291.8K"
    assert _fmt_k(12345.6, 0) == "$12,346K"   # colorbar uses 0 decimals
    print("ok  _fmt_k (Skylit $X,XXX.XK formatting)")


def test_build_greek_matrix_missing_is_nan():
    # Two expiries sharing some strikes; a strike absent for one expiry must be NaN
    # (a dark gap), NOT 0 — so it stays distinct from a present near-zero cell.
    per_exp = {"06-08\nD0": {100.0: 5e6, 101.0: -2e6}, "06-09\nD1": {100.0: 3e6}}
    window = [99.0, 100.0, 101.0]
    mat = build_greek_matrix(per_exp, window, ["06-08\nD0", "06-09\nD1"])
    assert list(mat.index) == [101.0, 100.0, 99.0]   # rows descending
    assert mat.loc[100.0, "06-08\nD0"] == 5000.0     # scaled to $K (5e6 -> 5,000K)
    assert pd.isna(mat.loc[101.0, "06-09\nD1"])      # absent strike -> NaN
    assert pd.isna(mat.loc[99.0, "06-08\nD0"])       # absent everywhere -> NaN
    print("ok  build_greek_matrix (missing -> NaN, $K scaling, descending rows)")


def test_annot_grid_king_and_blanks():
    mat = build_greek_matrix(
        {"A": {100.0: 250e6, 101.0: -3e6}, "B": {100.0: 0.2e6}},
        [99.0, 100.0, 101.0], ["A", "B"])
    annot = _annot_grid(mat, decimals=1)
    flat = [s for row in annot for s in row]
    kings = [s for s in flat if SKY_KING in s]
    assert len(kings) == 1 and kings[0].startswith("$250,000.0K")  # single King, K-formatted
    # Present near-zero (0.2M << 0.5% of 250M King) is blanked; absent cells are blank.
    a_idx = list(mat.index)
    assert annot[a_idx.index(100.0)][list(mat.columns).index("B")] == ""
    assert annot[a_idx.index(99.0)][list(mat.columns).index("A")] == ""
    # A shown non-King value formats Skylit-style ($X,XXX.XK / -$X,XXX.XK).
    neg = annot[a_idx.index(101.0)][list(mat.columns).index("A")]
    assert neg == "-$3,000.0K", neg
    print("ok  _annot_grid (one King, K-formatted, near-zero + missing blanked)")


def test_render_grid_smoke():
    import tempfile
    spot = 100.4
    # Mixed signs + a present zero + missing cells must all render without error.
    mat = build_greek_matrix(
        {"A": {99.0: -4e6, 100.0: 0.0, 101.0: 7e6}, "B": {100.0: 2e6}},
        [99.0, 100.0, 101.0, 102.0], ["A", "B"])
    # Spot line boundary = count of window strikes strictly above spot.
    n_above = sum(1 for k in mat.index if k > spot)
    assert n_above == 2   # 101, 102 are above 100.4
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "smoke.png")
        render_grid(mat, spot, "smoke", "$M", path, decimals=1)
        assert os.path.exists(path) and os.path.getsize(path) > 0
        # An all-NaN (no data) grid should also render without throwing.
        empty = build_greek_matrix({}, [99.0, 100.0], ["A"])
        p2 = os.path.join(d, "empty.png")
        render_grid(empty, spot, "empty", "$M", p2, decimals=1)
        assert os.path.exists(p2)
    print("ok  render_grid smoke (mixed/zero/missing + all-NaN)")


def test_summary_table_and_text_smoke():
    import tempfile
    rows = [
        {"exp": "2026-06-08", "dte": 2, "regime": "negative",
         "keys": {"gamma_flip": 0.0, "call_wall": 755.0, "put_wall": 745.0},
         "flip_s": "n/a", "cw_s": "$755", "pw_s": "$745",
         "net_gex": -688802.0, "net_vex": -572324.0, "net_cex": -6986.0},
        {"exp": "2026-06-09", "dte": 3, "regime": "positive",
         "keys": {"gamma_flip": 738.0, "call_wall": 755.0, "put_wall": 730.0},
         "flip_s": "$738", "cw_s": "$755", "pw_s": "$730",
         "net_gex": -298314.0, "net_vex": 876294.0, "net_cex": -24701.0},
    ]
    et = datetime(2026, 6, 6, 1, 40)
    # Header caption carries the title + magnet read but NOT the fixed-width table.
    text = build_summary_text(100.0, "cboe", None, et, rows)
    assert "SPY Dealerflow" in text and "```" not in text and "ΣGEX" not in text
    # The local report artifact still embeds the plain-text table + legend.
    report = build_summary(100.0, "cboe", None, et, rows)
    assert "```" in report and "ΣVanna" in report and "per 1.00σ" in report
    # The table renders to a non-empty PNG (mixed signs, an n/a flip, zebra rows).
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "summary.png")
        render_summary_table(rows, path)
        assert os.path.exists(path) and os.path.getsize(path) > 0
    print("ok  summary table PNG + header/report text")


def test_slot_decision():
    # Weekend / holiday -> skip regardless of cron.
    assert slot_decision(datetime(2026, 6, 6, 10, 0), force=False, cron="0 14 * * 1-5")[0] == "skip"
    # --force always runs (slot is None).
    assert slot_decision(datetime(2026, 6, 6, 10, 0), force=True, cron="")[0] == "force"
    # A valid EDT cron on a normal session maps to its intended slot and runs.
    action, slot = slot_decision(datetime(2026, 6, 5, 10, 0), force=False, cron="0 14 * * 1-5")
    assert action == "run" and slot == (10, 0)
    # The winter-morning cron self-skips on a summer date (14:31 UTC -> 10:31 EDT, not a
    # slot), so the dual EST/EDT crons never double-post.
    assert slot_decision(datetime(2026, 6, 5, 10, 31), force=False, cron="31 14 * * 1-5")[0] == "skip"
    print("ok  slot_decision")


if __name__ == "__main__":
    test_select_window_strikes()
    test_cumulative_zero_cross()
    test_exposure_dealer_sign()
    test_vex_per_full_sigma()
    test_eastern_now()
    test_fmt_k_skylit_style()
    test_build_greek_matrix_missing_is_nan()
    test_annot_grid_king_and_blanks()
    test_render_grid_smoke()
    test_summary_table_and_text_smoke()
    test_spy_hourly_slots_dedupe()
    test_spy_early_close_suppresses_afternoon()
    test_early_close_calendar()
    test_slot_decision()
    print("\nAll spy_gex tests passed.")
