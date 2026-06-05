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
from src.scorer import score_components, weighted_score, dominant_edge
from src.strategy_generator import generate_strategy
from src.utils import load_previous_close, save_current_close, send_discord
from src.cboe_source import fetch_cboe

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
                ax=axes[1], cbar_kws={"label": "$M / vol-pt"})
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


def build_pick_message(rank, contract, keys, regime, mode, date):
    tchar = "C" if contract["type"] == "call" else "P"
    vanna_regime = contract.get("vanna_regime", "neutral")
    bullets = generate_strategy(contract, keys, regime, contract["score"], vanna_regime)
    msg = f"# 🔥 HIGH CONVICTION #{rank} — {mode.upper()} {date}\n"
    msg += f"## {contract['ticker']} {contract['strike']:.2f}{tchar} {contract['exp']} — Score {contract['score']:.1f}\n"
    msg += (f"**Regime:** {regime} (gamma) / {vanna_regime} (vanna) | **Edge:** {contract.get('edge', '-')} | "
            f"**Est. Premium:** ${contract['premium_est']:.0f}K | **Vol/OI:** {contract['vol_oi']:.1f} | "
            f"**OTM:** {contract['otm']:.1f}% | **DTE:** {contract.get('dte', '?')} | "
            f"**Data:** {contract.get('source', 'yfinance')}\n")
    msg += (f"**Gamma levels:** flip {keys['gamma_flip']:.2f} · "
            f"call wall {keys['call_wall']:.2f} · put wall {keys['put_wall']:.2f}\n")
    msg += (f"**Vanna/Charm:** vanna flip {contract.get('vanna_flip', 0.0):.2f} · "
            f"Σvanna {contract.get('total_vex_m', 0.0):.1f}M/vol-pt · "
            f"Σcharm {contract.get('total_cex_m', 0.0):.1f}M/day\n\n")
    msg += "**Entry/Exit Strategy:**\n" + "\n".join(f"- {b}" for b in bullets)
    return msg


def build_table_message(lower_conv, mode, date):
    msg = f"# 📊 Additional Candidates — {mode.upper()} {date}\n"
    if not lower_conv:
        return msg + "\n_No additional candidates today._"
    header = f"{'Ticker':<7}{'C/P':<4}{'Strike':>9}{'DTE':>5}{'Score':>7}{'Vol/OI':>8}  Edge"
    rows = [header, "-" * len(header)]
    for contract, *_ in lower_conv:
        tchar = "C" if contract["type"] == "call" else "P"
        rows.append(f"{contract['ticker']:<7}{tchar:<4}{contract['strike']:>9.2f}"
                    f"{contract.get('dte', '?'):>5}{contract['score']:>7.1f}"
                    f"{contract['vol_oi']:>8.1f}  {contract.get('edge', '')}")
    return msg + "```\n" + "\n".join(rows) + "\n```"


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


def main(mode: str):
    print(f"🚀 Running {mode.upper()} mode at {datetime.now()}")
    results = []
    previous = load_previous_close() if mode == "morning" else None

    for ticker in config["watchlist"]:
        try:
            got = get_chains(ticker)
            if not got:
                print(f"Skipping {ticker}: no data")
                continue
            spot, chains, source = got
            for exp_str, df in sorted(chains.items()):
                # Skip anything outside the DTE window, and any same-day expiry that
                # has already passed today's 16:00 close (a 0-DTE close run must not
                # score contracts that are effectively expired).
                exp_close = datetime.strptime(exp_str, "%Y-%m-%d").replace(hour=16, minute=0)
                dte = (exp_close.date() - datetime.now().date()).days
                if dte < 0 or dte > config["dte_max"] or datetime.now() >= exp_close:
                    continue
                gex_grid, vex_grid, cex_grid = compute_exposure_grids(df, spot, exp_str)
                if not gex_grid:
                    continue  # no usable open interest; exposures would be meaningless
                key_levels = get_key_levels(gex_grid, spot)
                regime = get_regime(gex_grid)
                vanna_regime = get_vanna_regime(vex_grid)
                vanna_flip = cumulative_zero_cross(vex_grid, spot)
                total_vex_m = sum(vex_grid.values()) / 1e6
                total_cex_m = sum(cex_grid.values()) / 1e6
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
                    score = weighted_score(comps, config["score_weights"])
                    if score >= 60:
                        contract = {
                            "ticker": ticker,
                            "strike": strike,
                            "type": row.get("opt_type", "call"),
                            "exp": exp_str,
                            "dte": dte,
                            "spot": _f(spot),
                            "source": source,
                            "score": _f(score),
                            "edge": dominant_edge(comps, config["score_weights"]),
                            "premium_est": _f(premium_est),
                            "vol_oi": _f(vol / oi) if oi else 0.0,
                            "otm": _f(abs(strike - spot) / spot * 100) if spot else 0.0,
                            "vanna_regime": vanna_regime,
                            "vanna_flip": _f(vanna_flip),
                            "total_vex_m": _f(total_vex_m),
                            "total_cex_m": _f(total_cex_m),
                            "vanna_ex": _f(strike_vex),
                            "charm_ex": _f(strike_cex),
                        }
                        results.append((contract, gex_grid, vex_grid, key_levels, regime))
            time.sleep(0.5)  # gentle pacing across the watchlist
        except Exception as e:
            print(f"Skipping {ticker}: {e}")
            continue

    # Sort all candidates by score, then keep only each ticker's single best contract
    # so the two high-conviction picks — and the additional-candidates table — are
    # diversified across underlyings. This is the guardrail that prevents e.g. both
    # picks being META (640C and 650C); META takes one slot, the next pick must be a
    # different ticker.
    results.sort(key=lambda x: x[0]["score"], reverse=True)
    best_by_ticker, seen = [], set()
    for r in results:
        tk = r[0]["ticker"]
        if tk not in seen:
            seen.add(tk)
            best_by_ticker.append(r)
    cutoff = config["high_conviction_cutoff"]
    high_conv = [r for r in best_by_ticker if r[0]["score"] >= cutoff][:2]
    high_tickers = {r[0]["ticker"] for r in high_conv}
    lower_conv = [r for r in best_by_ticker
                  if r[0]["ticker"] not in high_tickers and r[0]["score"] >= 60][:8]

    date = datetime.now().date()
    sections = [f"# 🚀 Options High-Conviction Screener\n**Run:** {mode.upper()} | **Date:** {date}\n"]
    if mode == "morning" and previous:
        sections.append("**MORNING UPDATE** – Pre-market spot applied + GEX shift alerts\n")

    # Messages 1 & 2: each high-conviction pick gets its own gamma+vanna heatmap.
    for i, (contract, gex_grid, vex_grid, keys, regime) in enumerate(high_conv, 1):
        png = f"gex_heatmap_{i}.png"
        render_heatmap(contract, gex_grid, vex_grid, png)
        msg = build_pick_message(i, contract, keys, regime, mode, date)
        sections.append(msg)
        send_discord(msg, png)
        time.sleep(1)  # be gentle with Discord rate limits between messages

    if not high_conv:
        none_msg = f"# 🚀 Options Screener — {mode.upper()} {date}\n\n_No high-conviction setups today._"
        sections.append(none_msg)
        send_discord(none_msg)
        time.sleep(1)

    # Message 3: additional-candidates table (ticker / type / strike / dte / score /
    # vol-OI / edge) — one row per remaining ticker, ranked by score.
    table_msg = build_table_message(lower_conv, mode, date)
    sections.append(table_msg)
    send_discord(table_msg)

    with open("report.md", "w") as f:
        f.write("\n\n".join(sections))

    save_current_close(results)
    print("✅ Reports generated, heatmaps saved, Discord messages sent")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["close", "morning"], required=True)
    args = parser.parse_args()
    main(args.mode)
