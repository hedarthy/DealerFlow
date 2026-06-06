"""Offline tests for the on-demand ticker GEX alert (no network, no Discord import).

Run with:  python -m ticker_gex.tests.test_ticker_gex   (from the repo root)

Verifies ticker validation, the vendored math (VEX per-1.00 sigma), the $K formatting,
the $K/NaN greek matrix, the King-starred annotations, render smokes for all five images,
on-demand expiry selection, and the engine's render/no-data/invalid paths with a
monkeypatched (network-free) data source. Importing ``ticker_gex.engine`` must NOT pull
in ``discord`` — that dependency belongs only to the bot.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ticker_gex.exposure import bs_greeks, contract_exposures
from ticker_gex.calendar_util import eastern_now
from ticker_gex.agent import (
    select_expiries, build_greek_matrix, _annot_grid, render_grid, render_summary_table,
    render_front_triptych, build_summary_text, build_summary, _fmt_k, SKY_KING,
    WINDOW_STRIKES,
)
from ticker_gex.exposure import compute_exposure_grids, get_key_levels, select_window_strikes
from ticker_gex import data_source as ds
from ticker_gex import notify as nt
from ticker_gex import engine
from ticker_gex.engine import validate_ticker, run_for_ticker


# --------------------------------------------------------------------------- validation

def test_validate_ticker():
    assert validate_ticker("TSLA") == ("TSLA", None)
    assert validate_ticker("tsla")[0] == "TSLA"          # lowercase -> normalised
    assert validate_ticker("  spy ")[0] == "SPY"         # whitespace trimmed
    assert validate_ticker("$QQQ")[0] == "QQQ"           # leading $ stripped
    assert validate_ticker("BRK.B")[0] == "BRK.B"        # dotted share class allowed
    for junk in ("", "   ", None, "TS LA", "TS!A", "TOOLONG", "123", ".SPX"):
        sym, err = validate_ticker(junk)
        assert sym is None and err, f"expected reject for {junk!r}"
    print("ok  validate_ticker (normalise valid, reject junk/too-long/empty)")


def test_fmt_k_skylit_style():
    assert _fmt_k(1000.0) == "$1,000.0K"
    assert _fmt_k(-41686.1) == "-$41,686.1K"
    assert _fmt_k(0.0) == "$0.0K"
    assert _fmt_k(2458291.8) == "$2,458,291.8K"
    assert _fmt_k(12345.6, 0) == "$12,346K"
    print("ok  _fmt_k (Skylit $X,XXX.XK formatting)")


def test_vex_per_full_sigma():
    # VEX is quoted per a FULL 1.00 sigma move in IV (one whole vol point), NOT per
    # 0.01 vol-point: vex == vanna * (oi*100*sign) * spot with NO 0.01 factor.
    spot, strike, T, iv, oi, sign = 100.0, 105.0, 30 / 365.25, 0.18, 5000, 1
    _, vanna, _ = bs_greeks(spot, strike, T, 0.05, iv)
    _, vex, _ = contract_exposures(spot, strike, T, iv, oi, sign)
    expected = vanna * (oi * 100 * sign) * spot
    assert abs(vex - expected) < 1e-9
    assert abs(vex - 100.0 * (expected * 0.01)) < 1e-9
    print("ok  VEX scaled per 1.00 sigma (full vol point, no 0.01)")


def test_build_greek_matrix_missing_is_nan():
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
    assert len(kings) == 1 and kings[0].startswith("$250,000.0K")
    a_idx = list(mat.index)
    assert annot[a_idx.index(100.0)][list(mat.columns).index("B")] == ""
    assert annot[a_idx.index(99.0)][list(mat.columns).index("A")] == ""
    neg = annot[a_idx.index(101.0)][list(mat.columns).index("A")]
    assert neg == "-$3,000.0K", neg
    print("ok  _annot_grid (one King, K-formatted, near-zero + missing blanked)")


# --------------------------------------------------------------------------- expiries

def test_select_expiries_on_demand():
    base = datetime(2026, 6, 5, 10, 0)   # 10:00 ET, before the 16:00 close
    today = base.date()

    def d(n):
        return (today + timedelta(days=n)).strftime("%Y-%m-%d")

    chains = {d(-2): None, d(-1): None, d(0): None, d(1): None,
              d(2): None, d(3): None, d(6): None, d(9): None}
    picks = select_expiries(chains, base)
    exps = [e for e, _ in picks]
    assert d(-1) not in exps and d(-2) not in exps          # past expiries dropped
    assert exps == [d(0), d(1), d(2), d(3), d(6)]           # 5 nearest on/after today
    assert picks[0] == (d(0), 0)                            # 0DTE kept before close

    after = datetime(2026, 6, 5, 16, 0)                     # at the close -> drop 0DTE
    picks2 = select_expiries(chains, after)
    assert picks2[0][0] == d(1)
    print("ok  select_expiries (5 nearest on/after; 0DTE only before 16:00 ET)")


# --------------------------------------------------------------------------- render smokes

def test_render_grid_smoke():
    spot = 100.4
    mat = build_greek_matrix(
        {"A": {99.0: -4e6, 100.0: 0.0, 101.0: 7e6}, "B": {100.0: 2e6}},
        [99.0, 100.0, 101.0, 102.0], ["A", "B"])
    assert sum(1 for k in mat.index if k > spot) == 2
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "smoke.png")
        render_grid(mat, spot, "smoke", "$M", path, decimals=1)
        assert os.path.exists(path) and os.path.getsize(path) > 0
        empty = build_greek_matrix({}, [99.0, 100.0], ["A"])
        p2 = os.path.join(d, "empty.png")
        render_grid(empty, spot, "empty", "$M", p2, decimals=1)
        assert os.path.exists(p2)
    print("ok  render_grid smoke (mixed/zero/missing + all-NaN)")


def test_front_triptych_smoke():
    spot = 100.4
    window = [99.0, 100.0, 101.0, 102.0]
    front = "06-08\nD2"
    per_exp = {
        "gex": {front: {99.0: -4e6, 100.0: 1e6, 101.0: 7e6}},
        "vex": {front: {99.0: -9e8, 100.0: 2e8, 101.0: 5e8, 102.0: -1e8}},
        "cex": {front: {99.0: -3e4, 100.0: 0.0, 101.0: 8e4}},
    }
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "triptych.png")
        render_front_triptych(per_exp, window, front, spot, "front triptych", path)
        assert os.path.exists(path) and os.path.getsize(path) > 0
    print("ok  render_front_triptych smoke (3 greeks side by side, own scales)")


def test_summary_table_and_text_smoke():
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
    et = datetime(2026, 6, 6, 13, 40)
    text = build_summary_text("TSLA", 100.0, "cboe", et, rows)
    assert "TSLA Dealerflow" in text and "```" not in text and "\u03a3GEX" not in text
    report = build_summary("TSLA", 100.0, "cboe", et, rows)
    assert "```" in report and "\u03a3Vanna" in report and "per 1.00\u03c3" in report
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "summary.png")
        render_summary_table(rows, path, "TSLA", et, 737.55, None)
        assert os.path.exists(path) and os.path.getsize(path) > 0
    print("ok  summary table PNG + header/report text (generation time, no slot)")


# --------------------------------------------------------------------------- engine

def _synthetic_chains(spot=100.0):
    """A small offline option chain spanning the 5 nearest expiries (network-free)."""
    today = eastern_now().date()
    chains = {}
    for n in (1, 2, 3, 4, 5):
        exp = (today + timedelta(days=n)).strftime("%Y-%m-%d")
        rows = []
        for k in range(90, 111):
            rows.append({"strike": float(k), "opt_type": "call", "openInterest": 1000,
                         "impliedVolatility": 0.25, "volume": 200, "lastPrice": 1.5,
                         "contractSymbol": f"X{n}C{k}"})
            rows.append({"strike": float(k), "opt_type": "put", "openInterest": 1200,
                         "impliedVolatility": 0.27, "volume": 200, "lastPrice": 1.5,
                         "contractSymbol": f"X{n}P{k}"})
        chains[exp] = pd.DataFrame(rows)
    return spot, chains, "cboe"


class _patch:
    """Tiny attribute monkeypatch context manager (restores on exit)."""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.orig = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.orig)


def test_run_for_ticker_render_only():
    spot, chains, source = _synthetic_chains()
    calls = {"send": 0}

    def boom(*a, **k):
        calls["send"] += 1  # must NOT be called when webhook_url is None

    with _patch(ds, "get_chains", lambda t: (spot, chains, source)), \
         _patch(nt, "send_discord", boom), \
         tempfile.TemporaryDirectory() as d:
        res = run_for_ticker("tsla", webhook_url=None, out_dir=d)
        assert res.ok and res.ticker == "TSLA" and res.source == "cboe"
        assert res.posted == 0 and calls["send"] == 0       # no webhook -> no posting
        assert len(res.images) == 5
        for p in res.images:
            assert os.path.exists(p) and os.path.getsize(p) > 0
            assert os.path.basename(p).startswith("TSLA_")  # namespaced per request
    print("ok  run_for_ticker render-only (5 namespaced PNGs, posts nothing)")


def test_run_for_ticker_posts_with_webhook():
    spot, chains, source = _synthetic_chains()
    sent = []

    def fake_send(content, png_path=None, webhook_url=None):
        sent.append((content, png_path, webhook_url))

    with _patch(ds, "get_chains", lambda t: (spot, chains, source)), \
         _patch(nt, "send_discord", fake_send), \
         _patch(engine.time, "sleep", lambda _s: None), \
         tempfile.TemporaryDirectory() as d:
        res = run_for_ticker("QQQ", webhook_url="https://example.invalid/webhook", out_dir=d)
        assert res.ok and res.posted == 5 and len(sent) == 5
        assert all(w == "https://example.invalid/webhook" for _, _, w in sent)
        # Post order: summary card first, triptych last; every message carries an image.
        assert all(png and os.path.exists(png) for _, png, _ in sent)
    print("ok  run_for_ticker posts 5 messages to the supplied webhook")


def test_run_for_ticker_no_data_graceful():
    with _patch(ds, "get_chains", lambda t: None), tempfile.TemporaryDirectory() as d:
        res = run_for_ticker("ZZZZ", webhook_url=None, out_dir=d)
        assert not res.ok and res.error == "no_data" and "ZZZZ" in res.message
        assert res.posted == 0  # never crashed, never posted
    print("ok  run_for_ticker graceful on unfetchable ticker (no crash)")


def test_run_for_ticker_invalid_ticker():
    res = run_for_ticker("ZZ ZZ", webhook_url=None)
    assert not res.ok and res.phase == "validate" and res.posted == 0
    print("ok  run_for_ticker rejects junk ticker before any fetch/render")


def test_engine_has_no_discord_dependency():
    # Importing the engine (done at module top) must not drag in discord or the bot.
    assert "discord" not in sys.modules, "engine import pulled in discord"
    assert "ticker_gex.bot" not in sys.modules, "engine import pulled in the bot"
    print("ok  engine import is free of discord / bot")


if __name__ == "__main__":
    test_validate_ticker()
    test_fmt_k_skylit_style()
    test_vex_per_full_sigma()
    test_build_greek_matrix_missing_is_nan()
    test_annot_grid_king_and_blanks()
    test_select_expiries_on_demand()
    test_render_grid_smoke()
    test_front_triptych_smoke()
    test_summary_table_and_text_smoke()
    test_run_for_ticker_render_only()
    test_run_for_ticker_posts_with_webhook()
    test_run_for_ticker_no_data_graceful()
    test_run_for_ticker_invalid_ticker()
    test_engine_has_no_discord_dependency()
    print("\nAll ticker_gex tests passed.")
