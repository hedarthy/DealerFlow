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

load_dotenv()

with open("config.json") as f:
    config = json.load(f)


def main(mode: str):
    print(f"🚀 Running {mode.upper()} mode at {datetime.now()}")
    results = []
    previous = load_previous_close() if mode == "morning" else None

    for ticker in config["watchlist"]:
        try:
            stock = yf.Ticker(ticker)
            spot = stock.history(period="1d")["Close"].iloc[-1]
            expirations = stock.options[:4]  # nearest 4 expirations
            for exp_str in expirations:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d")
                if (exp_date - datetime.now()).days > config["dte_max"]:
                    continue
                chain = stock.option_chain(exp_str)
                df = pd.concat([chain.calls, chain.puts])
                gex_grid = compute_gex_grid(df, spot)
                key_levels = get_key_levels(gex_grid)
                regime = get_regime(gex_grid)

                for _, row in df.iterrows():
                    score = compute_composite_score(row, gex_grid, spot, regime, config["score_weights"])
                    if score >= 60 and row.get("volume", 0) >= config["min_volume"]:
                        contract = {
                            "ticker": ticker,
                            "strike": row["strike"],
                            "type": "call" if row.name in chain.calls.index else "put",
                            "exp": exp_str,
                            "score": score,
                            "premium_est": row.get("volume", 0) * row.get("lastPrice", 0) * 100 / 1000,
                            "vol_oi": row.get("volume", 0) / row.get("openInterest", 1),
                            "otm": abs(row["strike"] - spot) / spot * 100
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
        report += f"### {i}. {contract['ticker']} {contract['strike']}{'C' if contract['type']=='call' else 'P'} {contract['exp']} – Score **{contract['score']:.0f}**\n"
        report += f"**Est. Premium:** ${contract['premium_est']:.0f}K | **Vol/OI:** {contract['vol_oi']:.1f} | **OTM:** {contract['otm']:.1f}% | **DTE:** {config['dte_max']}\n\n"
        report += "**Entry/Exit Strategy:**\n" + "\n".join([f"- {b}" for b in bullets]) + "\n\n"

    report += "## 📉 Lower Conviction Trades (ranked descending)\n"
    for i, (contract, _, _, _) in enumerate(lower_conv, 1):
        report += f"{i}. {contract['ticker']} {contract['strike']}{'C/P'} – Score **{contract['score']:.0f}** (Vol/OI {contract['vol_oi']:.1f})\n"

    # Save heatmap PNG (latest high-conv)
    if high_conv:
        plt.figure(figsize=(12, 7))
        sns.heatmap(pd.DataFrame(list(high_conv[0][1].items()), columns=["strike", "gex"]).set_index("strike"),
                    annot=True, cmap="RdYlGn", center=0, fmt=".0f")
        plt.title(f"GEX Heatmap – {high_conv[0][0]['ticker']} High Conviction")
        plt.savefig("gex_heatmap.png")
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
