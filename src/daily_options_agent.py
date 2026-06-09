import json, argparse, os, time
from datetime import datetime
from dotenv import load_dotenv
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from src.gex_calculator import (
    compute_exposure_grids, cumulative_zero_cross,
    get_key_levels, get_regime, get_vanna_regime, _expiry_T,
)
from src.scorer import (
    score_components, weighted_score, dominant_edge, price_action_adjustment,
    gex_directional_adjustment, vanna_directional_adjustment,
    flow_imbalance_adjustment, aggregate_conviction,
)
from src.strategy_generator import generate_strategy
from src.utils import load_previous_close, save_current_close, send_discord
from src.cboe_source import fetch_cboe
from src.timeutil import eastern_now
from src.market_calendar import is_trading_day, cron_scheduled_et_hour, INTENDED_ET_HOUR
from src.event_calendar import ticker_event_dates, event_in_window
from src.heatmap import render_pick_triptych, render_candidates_table

# Resolve all relative paths (config.json, report.md, artifacts, .env) from repo root,
# so the agent behaves identically regardless of the caller's working directory.
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

with open("config.json") as f:
    config = json.load(f)


def _f(x, default=0.0):
    """Coerce to float, mapping None/non-numeric/NaN to a default."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # v != v is True only for NaN


def _flow_premium_split(chains, dte_max):
    """Sum the day's traded option premium (vol × lastPrice × 100), split call vs put,
    across every expiry inside the DTE window.

    A per-ticker order-flow proxy: heavy $-premium skewed into calls (puts) is a bullish
    (bearish) lean. Computed once per ticker and fed to ``flow_imbalance_adjustment``.
    Returns ``(call_premium, put_premium)`` in raw dollars.
    """
    call_prem = put_prem = 0.0
    today = eastern_now().date()
    for exp_str, df in chains.items():
        try:
            exp_close = datetime.strptime(exp_str, "%Y-%m-%d").replace(hour=16, minute=0)
        except (ValueError, TypeError):
            continue
        dte = (exp_close.date() - today).days
        if dte < 0 or dte > dte_max:
            continue
        for _, row in df.iterrows():
            prem = max(0.0, _f(row.get("volume", 0))) * max(0.0, _f(row.get("lastPrice", 0))) * 100.0
            if str(row.get("opt_type", "call")).lower().startswith("c"):
                call_prem += prem
            else:
                put_prem += prem
    return call_prem, put_prem


def render_heatmap(contract, gex_grid, vex_grid, path):
    """Two-panel heatmap: gamma exposure (GEX) and vanna exposure (VEX) by strike,
    windowed to the 21 strikes nearest the trade and scaled to $millions."""
    center = contract["strike"]

    def window(grid):
        d = pd.DataFrame(list(grid.items()), columns=["strike", "val"])
        d["val"] = d["val"] / 1e6
        d["dist"] = (d["strike"] - center).abs()
        return d.nsmallest(21, "dist").sort_values("strike").set_index("strike")[["val"]]

    g = window(gex_grid).rename(columns={"val": "GEX"})
    v = window(vex_grid).rename(columns={"val": "VEX"})
    fig, axes = plt.subplots(1, 2, figsize=(9, 10))
    sns.heatmap(g, annot=True, cmap="RdYlGn", center=0, fmt=".1f",
                ax=axes[0], cbar_kws={"label": "$M / 1% spot"})
    axes[0].set_title("Gamma exposure")
    axes[0].set_xlabel("")
    axes[0].set_ylabel("Strike")
    sns.heatmap(v, annot=True, cmap="PuOr", center=0, fmt=".1f",
                ax=axes[1], cbar_kws={"label": "$M / 1.00σ"})
    axes[1].set_title("Vanna exposure")
    axes[1].set_xlabel("")
    axes[1].set_ylabel("")
    tchar = "C" if contract["type"] == "call" else "P"
    fig.suptitle(f"{contract['ticker']} {contract['strike']:.2f}{tchar} — spot {contract.get('spot', 0):.2f}")
    plt.tight_layout()
    plt.savefig(path, dpi=120)
    plt.close()


def _expected_move_pct(df, spot, exp_str):
    """ATM expected move %% = atm_IV * sqrt(T) * 100.

    Used to scale the moneyness score by how far the underlying can actually travel
    by expiry, so a 1%-OTM SPY strike and a 1%-OTM small-cap strike are judged on
    comparable footing. Picks the implied vol of the strike nearest spot (with real
    OI and IV); returns 0.0 when nothing usable is found so the scorer falls back.
    """
    T = _expiry_T(exp_str)
    best_iv, best_dist = 0.0, None
    for _, row in df.iterrows():
        iv = _f(row.get("impliedVolatility", 0))
        oi = _f(row.get("openInterest", 0))
        if iv <= 0 or oi <= 0:
            continue
        d = abs(_f(row.get("strike", 0)) - spot)
        if best_dist is None or d < best_dist:
            best_dist, best_iv = d, iv
    return best_iv * (T ** 0.5) * 100.0


def compute_emas(ticker, _cache={}):
    """8/21-period EMA of daily closes (the SeanTrades momentum stack), cached per ticker.

    Uses yfinance daily history regardless of the quote source, because CBOE's
    delayed-quote feed carries no price history. Returns ``(ema8, ema21, last_close)``;
    the EMAs are ``None`` when history is missing or too short for a stable 21-period
    EMA (the price-action overlay then abstains), and ``last_close`` is returned
    whenever any history exists so the caller can sanity-check the live spot against it.
    ``adjust=False`` gives the conventional recursive trading EMA.
    """
    if ticker in _cache:
        return _cache[ticker]
    res = (None, None, None)
    try:
        hist = yf.Ticker(ticker).history(period="3mo")
        close = hist["Close"].dropna() if not hist.empty else None
        if close is not None and len(close) >= 21:
            ema8 = float(close.ewm(span=8, adjust=False).mean().iloc[-1])
            ema21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
            res = (ema8, ema21, float(close.iloc[-1]))
        elif close is not None and len(close) >= 1:
            res = (None, None, float(close.iloc[-1]))
    except Exception:
        res = (None, None, None)
    _cache[ticker] = res
    return res


_REGIME_PLAIN = {
    "positive": "positive γ — dealers buy dips & sell rips, so expect mean-reversion and a range-bound grind",
    "negative": "negative γ — dealers chase price, so expect trending breakouts with follow-through",
}


def build_pick_message(rank, contract, keys, regime, mode, date):
    tchar = "CALL" if contract["type"] == "call" else "PUT"
    spot = contract.get("spot", 0.0) or 0.0
    vanna_regime = contract.get("vanna_regime", "neutral")
    bullets = generate_strategy(contract, keys, regime, contract["score"], vanna_regime)

    def rel(x):
        if not x:
            return "n/a"
        if not spot:
            return f"${x:.2f}"
        return f"${x:.2f} ({(x / spot - 1) * 100:+.1f}%)"

    strike = ("%g" % contract["strike"])
    msg = f"# 🔥 HIGH CONVICTION #{rank} — {mode.upper()} {date}\n"
    msg += f"## {contract['ticker']} ${strike} {tchar} · exp {contract['exp']} · Score {contract['score']:.0f}\n"
    plain = _REGIME_PLAIN.get(regime)
    msg += f"**Spot ${spot:.2f}**" + (f" — {plain}.\n" if plain else "\n")
    msg += (f"**Levels:** flip {rel(keys['gamma_flip'])} · call wall {rel(keys['call_wall'])} · "
            f"put wall {rel(keys['put_wall'])}\n")
    msg += "**Plan**\n" + "\n".join(f"- {b}" for b in bullets) + "\n"

    # Conviction read: drive Confirms / Caution off the four structured directional
    # signals that actually scored (EMA stack, GEX structure, vanna sign, order flow), so
    # the display matches the math. A signal with positive points confirms the side, a
    # negative one cautions; neutral/absent signals are omitted. The summary line shows
    # the regime-weighted conviction and how many signals agreed.
    confirms, cautions = [], []
    signal_specs = [
        (contract.get("pa_label"), contract.get("pa_pts", 0.0)),
        (contract.get("gd_label"), contract.get("gd_pts", 0.0)),
        (contract.get("vn_label"), contract.get("vn_pts", 0.0)),
        (contract.get("fl_label"), contract.get("fl_pts", 0.0)),
    ]
    for label, pts in signal_specs:
        if pts > 0:
            confirms.append(label)
        elif pts < 0:
            cautions.append(label)
        elif label == "EMAs mixed":
            cautions.append("price tangled in 8/21 EMAs — no trend")
        # pts == 0 otherwise is a neutral read and contributes to neither side
    aligned = int(contract.get("aligned", 0))
    opposed = int(contract.get("opposed", 0))
    conv = contract.get("conviction", 0.0)
    msg += (f"**Conviction:** {regime or '—'}-γ · {aligned} signal"
            f"{'s' if aligned != 1 else ''} aligned, {opposed} opposed · "
            f"{conv:+.1f} pts over base {contract.get('base_score', 0.0):.0f}\n")
    if confirms:
        msg += "**Confirms:** " + " · ".join(confirms) + "\n"
    if cautions:
        msg += "**Caution:** " + " · ".join(cautions) + "\n"

    msg += (f"_Vol/OI {contract['vol_oi']:.1f} · OTM {contract['otm']:.1f}% · DTE {contract.get('dte', '?')} · "
            f"~${contract['premium_est'] / 1000:.1f}M prem · {contract.get('source', 'yfinance')} · "
            f"Σvanna {contract.get('total_vex_m', 0.0):.1f}M/1.00σ · "
            f"Σcharm {contract.get('total_cex_m', 0.0):.1f}M/day_")
    return msg


def build_table_caption(lower_conv, mode, date):
    """Short markdown caption that accompanies the additional-candidates PNG card.

    The table itself is rendered as an image (``render_candidates_table``) because a
    wide monospace code block wraps and becomes unreadable on mobile Discord. This is
    just the heading + the event-risk explainer.
    """
    msg = f"# 📊 Additional Candidates — {mode.upper()} {date}\n"
    if not lower_conv:
        return msg + "\n_No additional candidates today._"
    msg += (f"_{len(lower_conv)} setup{'s' if len(lower_conv) != 1 else ''} just below "
            f"the high-conviction bar, ranked by score (table below)._")
    if any(c.get("event_risk") for c, *_ in lower_conv):
        msg += ("\n⚠️ **event-risk** names have a known catalyst (earnings / keynote / etc.) "
                "on or before expiry — dealer-gamma pinning breaks down across binary events, "
                "so they are excluded from high conviction. Trade only with an event thesis.")
    return msg


def build_table_message(lower_conv, mode, date):
    msg = f"# 📊 Additional Candidates — {mode.upper()} {date}\n"
    if not lower_conv:
        return msg + "\n_No additional candidates today._"
    header = f"{'Ticker':<7}{'C/P':<4}{'Strike':>9}{'OTM%':>7}{'DTE':>5}{'Score':>7}{'Vol/OI':>8}  Edge"
    rows = [header, "-" * len(header)]
    has_event = False
    for contract, *_ in lower_conv:
        tchar = "C" if contract["type"] == "call" else "P"
        if contract.get("event_risk"):
            has_event = True
            lbl = contract.get("event_label") or "event"
            edge = f"⚠ {lbl}-risk (no high-conv)"
        else:
            edge = contract.get("edge", "")
        rows.append(f"{contract['ticker']:<7}{tchar:<4}{contract['strike']:>9.2f}"
                    f"{contract.get('otm', 0.0):>7.1f}"
                    f"{contract.get('dte', '?'):>5}{contract['score']:>7.1f}"
                    f"{contract['vol_oi']:>8.1f}  {edge}")
    out = msg + "```\n" + "\n".join(rows) + "\n```"
    if has_event:
        out += ("\n⚠️ **event-risk** names have a known catalyst (earnings / keynote / etc.) "
                "on or before expiry — dealer-gamma pinning breaks down across binary events, "
                "so they are excluded from high conviction. Trade only with an event thesis.")
    return out


def get_chains(ticker):
    """Return (spot, {expiry: df}, source).

    Prefer CBOE (real exchange OI/IV) and fall back to yfinance so a CBOE outage or
    geo-block never takes the screener down. Both sources yield DataFrames with the
    same columns (strike, opt_type, openInterest, impliedVolatility, volume,
    lastPrice, contractSymbol) so downstream processing is source-agnostic.
    """
    cb = fetch_cboe(ticker)
    if cb:
        spot, chains = cb
        if spot > 0 and chains:
            return spot, chains, "cboe"
    stock = yf.Ticker(ticker)
    hist = stock.history(period="1d")
    if hist.empty:
        return None
    spot = _f(hist["Close"].iloc[-1])
    if spot <= 0:
        return None
    chains = {}
    for exp_str in stock.options[:6]:
        try:
            chain = stock.option_chain(exp_str)
            calls = chain.calls.copy()
            calls["opt_type"] = "call"
            puts = chain.puts.copy()
            puts["opt_type"] = "put"
            chains[exp_str] = pd.concat([calls, puts], ignore_index=True)
        except Exception:
            continue
    if not chains:
        return None
    return spot, chains, "yfinance"


def main(mode: str, force: bool = False):
    et = eastern_now()
    if not force:
        target_hour = INTENDED_ET_HOUR[mode]
        if not is_trading_day(et.date()):
            print(f"⏭️  SKIPPED: {et.date()} ({et:%A}) is not a US market trading day "
                  f"(weekend or NYSE holiday). mode={mode}.")
            return
        cron = os.getenv("SCHEDULED_CRON", "").strip()
        sched_hour = cron_scheduled_et_hour(cron, et.date()) if cron else None
        if sched_hour is not None:
            # Decide eligibility from the cron's SCHEDULED ET hour, not the
            # runner's actual start time. The off-season (wrong-DST) cron is
            # scheduled for a different ET hour and self-skips even if GitHub
            # delays it into the intended hour; the correct cron owns the run
            # even if it starts late.
            if sched_hour != target_hour:
                print(f"⏭️  SKIPPED: cron '{cron}' is scheduled for {sched_hour:02d}:00 ET "
                      f"(off-DST duplicate), not the intended {target_hour:02d}:00 ET. The "
                      f"matching cron handles today's {mode} run.")
                return
            if et.hour != target_hour:
                print(f"⚠️  NOTE: the {target_hour:02d}:00 ET {mode} cron started late "
                      f"(now {et:%H:%M} ET) — GitHub scheduler latency; proceeding anyway.")
        elif et.hour != target_hour:
            # No cron context (a non-forced manual run): fall back to the
            # intended-hour gate on the actual ET clock.
            print(f"⏭️  SKIPPED: {mode} run is gated to {target_hour:02d}:00 ET, but it is "
                  f"currently {et:%H:%M} ET (hour {et.hour}). Use --force to override.")
            return
    print(f"🚀 Running {mode.upper()} mode at {et} ET")
    results = []
    source_counts = {}
    previous = load_previous_close() if mode == "morning" else None
    pa_enabled = bool(config.get("enable_price_action_filter", False))
    gd_enabled = bool(config.get("enable_gex_directional_filter", False))
    vanna_enabled = bool(config.get("enable_vanna_directional_filter", False))
    flow_enabled = bool(config.get("enable_flow_imbalance_filter", False))
    adaptive = bool(config.get("regime_adaptive_weights", False))
    regime_weights = config.get("conviction_regime_weights") or None
    event_enabled = bool(config.get("enable_event_filter", False))
    event_dates_map = config.get("event_dates", {}) if event_enabled else {}

    for ticker in config["watchlist"]:
        try:
            got = get_chains(ticker)
            if not got:
                print(f"Skipping {ticker}: no data")
                continue
            spot, chains, source = got
            source_counts[source] = source_counts.get(source, 0) + 1
            # SeanTrades-style price-action stack: computed once per ticker (cached),
            # off the daily-close history. Drives the additive confirmation overlay and
            # the high-conviction trend gate below. The last close also lets us sanity-
            # check the CBOE spot — a large divergence flags a stale quote.
            ema8, ema21, last_close = compute_emas(ticker) if pa_enabled else (None, None, None)
            if last_close and spot and abs(spot / last_close - 1.0) > 0.25:
                print(f"⚠️  {ticker}: {source} spot {spot:.2f} diverges >25% from prior "
                      f"close {last_close:.2f} — possible stale quote")
            # Per-ticker signed order-flow proxy: the day's call vs put traded premium
            # across the DTE window. Computed once here and reused for every contract.
            ticker_call_prem, ticker_put_prem = (
                _flow_premium_split(chains, config["dte_max"]) if flow_enabled else (0.0, 0.0))
            # Known binary catalysts (manual config map + best-effort earnings) for this
            # ticker. Resolved once per name; the per-contract window check below decides
            # whether a given expiry actually straddles one.
            ticker_events = (ticker_event_dates(ticker, event_dates_map, enable_earnings=True)
                             if event_enabled else [])
            for exp_str, df in sorted(chains.items()):
                # Skip anything outside the DTE window, and any same-day expiry that
                # has already passed today's 16:00 close (a 0-DTE close run must not
                # score contracts that are effectively expired).
                exp_close = datetime.strptime(exp_str, "%Y-%m-%d").replace(hour=16, minute=0)
                dte = (exp_close.date() - eastern_now().date()).days
                if dte < 0 or dte > config["dte_max"] or eastern_now() >= exp_close:
                    continue
                # Per-CONTRACT event check over [today, this expiry] — never per-ticker to
                # the furthest expiry (that would falsely tag short-dated contracts that
                # close before the catalyst). Date-level on purpose: a same-day expiry on
                # an event date is flagged (event time isn't reliable; bias to caution). A
                # flagged expiry is barred from high conviction below and surfaced only as
                # a caution candidate.
                event_risk, event_label = (
                    event_in_window(ticker_events, eastern_now().date(), exp_close.date())
                    if event_enabled else (False, None))
                gex_grid, vex_grid, cex_grid = compute_exposure_grids(df, spot, exp_str)
                if not gex_grid:
                    continue  # no usable open interest; exposures would be meaningless
                key_levels = get_key_levels(gex_grid, spot)
                regime = get_regime(gex_grid)
                vanna_regime = get_vanna_regime(vex_grid)
                vanna_flip = cumulative_zero_cross(vex_grid, spot)
                total_vex_m = sum(vex_grid.values()) / 1e6
                total_cex_m = sum(cex_grid.values()) / 1e6
                net_vex = sum(vex_grid.values())
                gross_vex = sum(abs(x) for x in vex_grid.values())
                max_vex = max((abs(x) for x in vex_grid.values()), default=0.0)
                max_cex = max((abs(x) for x in cex_grid.values()), default=0.0)
                # Net dealer gamma balance in [-1, 1]: the scale-free regime signal
                # that replaces the old binary positive/negative string.
                gross_gex = sum(abs(x) for x in gex_grid.values())
                gex_balance = sum(gex_grid.values()) / gross_gex if gross_gex else 0.0
                em_pct = _expected_move_pct(df, spot, exp_str)

                for _, row in df.iterrows():
                    vol = _f(row.get("volume", 0))
                    oi = _f(row.get("openInterest", 0))
                    # Tradability gates: need real open interest, volume, and a minimum
                    # traded premium so we never surface illiquid / untradeable strikes.
                    if oi <= 0 or vol < config["min_volume"]:
                        continue
                    strike = _f(row["strike"])
                    premium_est = vol * _f(row.get("lastPrice", 0)) * 100 / 1000  # $K
                    if premium_est < config["min_premium_k"]:
                        continue
                    # Score against this strike's NET dealer vanna/charm (concentration),
                    # normalised by the chain's largest net strike exposure.
                    strike_vex = vex_grid.get(strike, 0.0)
                    strike_cex = cex_grid.get(strike, 0.0)
                    comps = score_components(
                        row, spot, dte=dte, gex_balance=gex_balance, em_pct=em_pct,
                        vanna_ex=strike_vex, charm_ex=strike_cex,
                        max_vex=max_vex, max_cex=max_cex,
                    )
                    base = weighted_score(comps, config["score_weights"])
                    # Dealer-greek gate first: the price-action overlay CONFIRMS strong
                    # setups and ranks them, it does not rescue weak ones over the line.
                    if base < 60:
                        continue
                    opt_type = row.get("opt_type", "call")
                    pa_pts, pa_label = (price_action_adjustment(opt_type, spot, ema8, ema21)
                                        if pa_enabled else (0.0, None))
                    # Dealer-positioning DIRECTIONAL read from the GEX structure (only
                    # asserted where the structure is reliable — see scorer).
                    gd_pts, gd_label = (gex_directional_adjustment(
                                            opt_type, spot, key_levels["gamma_flip"],
                                            key_levels["call_wall"], key_levels["put_wall"],
                                            regime, em_pct=em_pct)
                                        if gd_enabled else (0.0, None))
                    # Dealer-FLOW directional reads, now intrinsic to conviction: the sign
                    # of net dealer vanna, and the day's call-vs-put traded-premium skew.
                    vn_pts, vn_label = (vanna_directional_adjustment(
                                            opt_type, net_vex, gross_vex, regime)
                                        if vanna_enabled else (0.0, None))
                    fl_pts, fl_label = (flow_imbalance_adjustment(
                                            opt_type, ticker_call_prem, ticker_put_prem)
                                        if flow_enabled else (0.0, None))
                    # Regime-weighted conviction: lean on the GEX structure + dealer flow
                    # in a positive-γ (pinning) book, on momentum + flow in a negative-γ
                    # (trending) book. aligned/opposed count the unweighted signal signs
                    # and drive the confluence gate at selection.
                    conviction, aligned, opposed = aggregate_conviction(
                        pa_pts, gd_pts, vn_pts, fl_pts, regime,
                        weights=regime_weights, adaptive=adaptive)
                    raw = base + conviction  # rank on the raw conviction-adjusted value
                    score = min(100.0, max(0.0, raw))  # display/threshold value, clamped
                    contract = {
                        "ticker": ticker,
                        "strike": strike,
                        "type": opt_type,
                        "exp": exp_str,
                        "dte": dte,
                        "spot": _f(spot),
                        "source": source,
                        "score": _f(score),
                        "rank_score": _f(raw),
                        "base_score": _f(base),
                        "conviction": _f(conviction),
                        "aligned": int(aligned),
                        "opposed": int(opposed),
                        "edge": dominant_edge(comps, config["score_weights"]),
                        "moneyness": _f(comps["moneyness_dte"]),
                        "em_pct": _f(em_pct),
                        "pa_label": pa_label,
                        "pa_pts": _f(pa_pts),
                        "pa_opposed": pa_pts < 0,
                        "gd_label": gd_label,
                        "gd_pts": _f(gd_pts),
                        "gex_opposed": gd_pts < 0,
                        "vn_label": vn_label,
                        "vn_pts": _f(vn_pts),
                        "fl_label": fl_label,
                        "fl_pts": _f(fl_pts),
                        "ema8": ema8,
                        "ema21": ema21,
                        "premium_est": _f(premium_est),
                        "vol_oi": _f(vol / oi) if oi else 0.0,
                        "otm": _f(abs(strike - spot) / spot * 100) if spot else 0.0,
                        "vanna_regime": vanna_regime,
                        "vanna_flip": _f(vanna_flip),
                        "total_vex_m": _f(total_vex_m),
                        "total_cex_m": _f(total_cex_m),
                        "vanna_ex": _f(strike_vex),
                        "charm_ex": _f(strike_cex),
                        "event_risk": bool(event_risk),
                        "event_label": event_label,
                    }
                    results.append((contract, gex_grid, vex_grid, key_levels, regime, cex_grid))
            time.sleep(0.5)  # gentle pacing across the watchlist
        except Exception as e:
            print(f"Skipping {ticker}: {e}")
            continue

    # Rank on the raw overlay-adjusted score so a strong, trend-confirmed name isn't
    # flattened against the 100 ceiling, then keep each ticker's single best contract.
    results.sort(key=lambda x: x[0]["rank_score"], reverse=True)

    def _dedupe_by_ticker(rows):
        out, seen = [], set()
        for r in rows:
            tk = r[0]["ticker"]
            if tk not in seen:
                seen.add(tk)
                out.append(r)
        return out

    cutoff = config["high_conviction_cutoff"]
    min_mny = config.get("min_moneyness_high_conv", 0)
    overlays_on = pa_enabled or gd_enabled or vanna_enabled or flow_enabled
    # Minimum number of independent directional signals (EMA / GEX-structure / vanna
    # sign / order-flow) that must AGREE for a top-two pick. This is the core of the
    # redesign: conviction now means *confluence of dealer-flow reads*, not a single
    # confirmation clearing a threshold. Set to 1 in config to restore the old behavior.
    min_confluence = int(config.get("min_confluence_high_conv", 1))

    def _opposed(c):
        # A pick must not fight ANY enabled directional signal.
        return int(c.get("opposed", 0)) > 0

    def _confirmed(c):
        # ...and at least `min_confluence` independent signals must confirm it. This keeps
        # coin-flip directionals — a strong base score with no real read on call-vs-put —
        # out of the top two, and demands genuine agreement among the dealer-flow reads.
        return int(c.get("aligned", 0)) >= min_confluence

    # High conviction: clear the cutoff, be reachable within ~a day's expected move
    # (the moneyness floor keeps un-hittable far-OTM lottery strikes — which can top the
    # board on raw vanna concentration alone — out of the top two), fight NO directional
    # signal, and (when overlays are on) be confirmed by at least `min_confluence` of
    # them. We gate BEFORE the per-ticker dedupe so a ticker whose top raw contract is
    # counter-trend still contributes its best *confluent* contract instead of being
    # dropped entirely.
    eligible_high = [r for r in results
                     if r[0]["score"] >= cutoff and r[0].get("moneyness", 0) >= min_mny
                     and not _opposed(r[0])
                     and not r[0].get("event_risk")
                     and (_confirmed(r[0]) or not overlays_on)]
    high_conv = _dedupe_by_ticker(eligible_high)[:2]
    high_tickers = {r[0]["ticker"] for r in high_conv}
    # Additional candidates: names that fight neither overlay (counter-trend setups are
    # not surfaced at all), one row per remaining ticker.
    non_opposed = [r for r in results if not _opposed(r[0])]
    lower_conv = [r for r in _dedupe_by_ticker(non_opposed)
                  if r[0]["ticker"] not in high_tickers and r[0]["score"] >= 60][:8]

    date = eastern_now().date()
    header = f"# 🚀 Options High-Conviction Screener\n**Run:** {mode.upper()} | **Date:** {date}\n"
    if pa_enabled:
        header += ("**Screen:** high relative-volume momentum names (AI/semis/photonics watchlist), "
                   "with high-conviction picks confirmed by the 8/21 EMA price-action stack "
                   "(SeanTrades-style).\n")
    sections = [header]
    if mode == "morning" and previous:
        sections.append("**MORNING UPDATE** – Pre-market spot applied + GEX shift alerts\n")
    if mode == "morning" and pa_enabled:
        sections.append("**Price-action check:** only setups aligned with the 8/21 EMA stack "
                        "are eligible for high conviction.\n")

    # Messages 1 & 2: each high-conviction pick gets its own dealer-greek heatmap.
    # Prefer the Skylit-style GEX/VEX/CEX triptych; if it (or its deps) fail for any
    # reason, degrade gracefully — old 2-panel gamma+vanna heatmap, then text-only —
    # so a rendering hiccup never drops the actual alert.
    for i, (contract, gex_grid, vex_grid, keys, regime, cex_grid) in enumerate(high_conv, 1):
        png = f"gex_heatmap_{i}.png"
        msg = build_pick_message(i, contract, keys, regime, mode, date)
        sections.append(msg)
        image = png
        try:
            render_pick_triptych(contract, gex_grid, vex_grid, cex_grid, png)
        except Exception as e:
            print(f"⚠️  triptych render failed for pick {i} ({contract.get('ticker')}): {e} "
                  f"— falling back to the 2-panel heatmap")
            try:
                render_heatmap(contract, gex_grid, vex_grid, png)
            except Exception as e2:
                print(f"⚠️  2-panel heatmap also failed for pick {i}: {e2} — posting text only")
                image = None
        send_discord(msg, image)
        time.sleep(1)  # be gentle with Discord rate limits between messages

    if not high_conv:
        none_msg = f"# 🚀 Options Screener — {mode.upper()} {date}\n\n_No high-conviction setups today._"
        sections.append(none_msg)
        send_discord(none_msg)
        time.sleep(1)

    # Message 3: additional-candidates table. Rendered as a PNG card because wide
    # monospace code blocks wrap and become unreadable on mobile Discord; the full
    # plain-text table is still written to the local report.md artifact below. If the
    # render fails for any reason, degrade to posting the text table so the alert
    # never silently drops.
    table_caption = build_table_caption(lower_conv, mode, date)
    table_full = build_table_message(lower_conv, mode, date)
    sections.append(table_full)
    table_png = None
    if lower_conv:
        table_png = "candidates_table.png"
        try:
            render_candidates_table(lower_conv, mode, date, table_png)
        except Exception as e:
            print(f"⚠️  candidates table render failed: {e} — posting text only")
            table_png = None
    if table_png:
        send_discord(table_caption, table_png)
    else:
        send_discord(table_full)

    with open("report.md", "w") as f:
        f.write("\n\n".join(sections))

    save_current_close(results)
    coverage = ", ".join(f"{k}={v}" for k, v in sorted(source_counts.items())) or "none"
    print(f"📡 Quote sources: {coverage} (CBOE preferred; yfinance powers EMA history)")
    print("✅ Reports generated, heatmaps saved, Discord messages sent")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["close", "morning"], required=True)
    parser.add_argument("--force", action="store_true",
                        help="Bypass the trading-day / intended-ET-hour guard "
                             "(used for manual dispatch and local test runs).")
    args = parser.parse_args()
    main(args.mode, force=args.force)
