# Options Day-Trade Screener

Free, deterministic options day-trade screener. Runs twice daily via GitHub Actions,
producing exactly 2 high-conviction trade ideas plus lower-conviction ranks, a GEX
heatmap PNG, and a Discord alert. Uses free `yfinance` data across a photonics / AI /
semiconductor watchlist.

## How it works

| Run | Schedule (ET) | Command |
| --- | --- | --- |
| Close | Mon–Fri 4:05 PM | `python -m src.daily_options_agent --mode=close` |
| Morning | Mon–Fri 9:10 AM | `python -m src.daily_options_agent --mode=morning` |

Pipeline: pull option chains → compute a Gamma Exposure (GEX) grid and key levels
(gamma flip, call/put walls) → score each contract → select trades → render report +
heatmap → post to Discord.

## Project layout

```
.
├── .github/workflows/   # close-run.yml, morning-run.yml
├── src/
│   ├── daily_options_agent.py   # entrypoint / orchestration
│   ├── gex_calculator.py        # GEX grid, key levels, regime
│   ├── scorer.py                # composite score
│   ├── strategy_generator.py    # entry/target/stop bullets
│   └── utils.py                 # persistence + Discord
├── config.json          # watchlist + thresholds + score weights
├── requirements.txt
└── reports/             # artifacts (gitignored)
```

## Setup

### 1. Discord webhook
The code reads the webhook from the `DISCORD_WEBHOOK_URL` environment variable — it is
**never** hard-coded.

- **GitHub Actions:** add a repository secret named `DISCORD_WEBHOOK_URL`
  (Settings → Secrets and variables → Actions). Both workflows pass it through `env`.
- **Local:** `cp .env.example .env` and set `DISCORD_WEBHOOK_URL`. `.env` is gitignored.

### 2. Run locally

```bash
pip install -r requirements.txt
python -m src.daily_options_agent --mode=close
```

Run from the repo root so the `src` package resolves.

## Configuration

Edit `config.json`:

- `watchlist` — tickers to scan.
- `dte_max` — max days to expiration considered.
- `min_volume`, `min_premium_k` — liquidity filters.
- `high_conviction_cutoff` — score threshold for the top bucket.
- `score_weights` — weights for GEX regime, flow proxy, squeeze, vanna/charm, moneyness.

All logic is deterministic — no LLM.