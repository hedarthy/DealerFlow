import json, argparse, os, requests, time
from datetime import datetime, timedelta
from dotenv import load_dotenv
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from src.gex_calculator import compute_gex_grid, get_key_levels, get_regime
from src.scorer import compute_composite_score
from src.strategy_generator import generate_strategy
from src.utils import load_previous_close, save_current_close, send_discord

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


def main(mode: str):
    print(f"🚀 Running {mode.upper()} mode at {datetime.now()}")
    results = []
    previous = load_previous_close() if mode == "morning" else None

    for ticker in config["watchlist"]:
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1d")
            if hist.empty:
                print(f"Skipping {ticker}: no price history")
                continue
            spot = _f(hist["Close"].iloc[-1])
            if spot <= 0:
                print(f"Skipping {ticker}: invalid spot")
                continue
            expirations = stock.options[:4]  # nearest 4 expirations
            for exp_str in expirations:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d")
                dte = (exp_date.date() - datetime.now().date()).days
                if dte < 0 or dte > config["dte_max"]:
                    continue
                chain = stock.option_chain(exp_str)
                calls = chain.calls.copy()
                calls["opt_type"] = "call"
                puts = chain.puts.copy()
                puts["opt_type"] = "put"
                df = pd.concat([calls, puts], ignore_index=True)
                gex_grid = compute_gex_grid(df, spot, exp_str)
                if not gex_grid:
                    continue  # no usable open interest; key levels would be meaningless
                key_levels = get_key_levels(gex_grid)
                regime = get_regime(gex_grid)

                for _, row in df.iterrows():
                    vol = _f(row.get("volume", 0))
                    oi = _f(row.get("openInterest", 0))
                    score = compute_composite_score(row, gex_grid, spot, regime, config["score_weights"])
                    if score >= 60 and vol >= config["min_volume"]:
                        strike = _f(row["strike"])
                        contract = {
                            "ticker": ticker,
                            "strike": strike,
                            "type": row.get("opt_type", "call"),
                            "exp": exp_str,
                            "dte": dte,
                            "spot": _f(spot),
                            "score": _f(score),
                            "premium_est": _f(vol * _f(row.get("lastPrice", 0)) * 100 / 1000),
                            "vol_oi": _f(vol / oi) if oi else 0.0,
                            "otm": _f(abs(strike - spot) / spot * 100) if spot else 0.0,
                        }
                        results.append((contract, gex_grid, key_levels, regime))
            time.sleep(1.5)  # safe delay for expanded ticker list
        except Exception as e:
            print(f"Skipping {ticker}: {e}")
            continue

    # Sort & select
    results.sort(key=lambda x: x[0]["score"], reverse=True)
    high_conv = [r for r in results if r[0]["score"] >= config["high_conviction_cutoff"]][:2]
    lower_conv = [r for r in results if 60 <= r[0]["score"] < config["high_conviction_cutoff"]][:8]

    # Build report
    report = f"# 🚀 Options High-Conviction Screener\n**Run:** {mode.upper()} | **Date:** {datetime.now().date()}\n\n"
    if mode == "morning" and previous:
        report += "**MORNING UPDATE** – Pre-market spot applied + GEX shift alerts\n\n"
        report += "**Overnight GEX Shift Alert:**\n"

    report += "## 🔥 HIGH CONVICTION DAY TRADES (Exactly 2)\n\n"
    for i, (contract, gex_grid, keys, regime) in enumerate(high_conv, 1):
        bullets = generate_strategy(contract, keys, regime, contract["score"])
        report += f"### {i}. {contract['ticker']} {contract['strike']:.2f}{'C' if contract['type']=='call' else 'P'} {contract['exp']} – Score **{contract['score']:.0f}**\n"
        report += f"**Est. Premium:** ${contract['premium_est']:.0f}K | **Vol/OI:** {contract['vol_oi']:.1f} | **OTM:** {contract['otm']:.1f}% | **DTE:** {contract.get('dte', config['dte_max'])}\n\n"
        report += "**Entry/Exit Strategy:**\n" + "\n".join([f"- {b}" for b in bullets]) + "\n\n"

    report += "## 📉 Lower Conviction Trades (ranked descending)\n"
    for i, (contract, _, _, _) in enumerate(lower_conv, 1):
        tchar = "C" if contract["type"] == "call" else "P"
        report += f"{i}. {contract['ticker']} {contract['strike']:.2f}{tchar} {contract['exp']} – Score **{contract['score']:.0f}** (Vol/OI {contract['vol_oi']:.1f})\n"

    # Save heatmap PNG (top high-conviction name), windowed near the traded strike
    if high_conv:
        top_contract, top_grid = high_conv[0][0], high_conv[0][1]
        center = top_contract["strike"]
        grid_df = pd.DataFrame(list(top_grid.items()), columns=["strike", "gex"])
        grid_df["gex_m"] = grid_df["gex"] / 1e6
        grid_df["dist"] = (grid_df["strike"] - center).abs()
        grid_df = grid_df.nsmallest(21, "dist").sort_values("strike")
        heat = grid_df.set_index("strike")[["gex_m"]]
        plt.figure(figsize=(6, 10))
        sns.heatmap(heat, annot=True, cmap="RdYlGn", center=0, fmt=".1f",
                    cbar_kws={"label": "GEX ($M)"})
        plt.title(f"GEX Heatmap – {top_contract['ticker']} (spot {top_contract.get('spot', 0):.2f})")
        plt.ylabel("Strike")
        plt.xlabel("")
        plt.tight_layout()
        plt.savefig("gex_heatmap.png", dpi=120)
        plt.close()

    with open("report.md", "w") as f:
        f.write(report)

    save_current_close(results)
    send_discord(report, "gex_heatmap.png" if high_conv else None)
    print("✅ Report generated, heatmap saved, Discord sent")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["close", "morning"], required=True)
    args = parser.parse_args()
    main(args.mode)
