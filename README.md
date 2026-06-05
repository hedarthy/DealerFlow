# Options Day-Trade Screener

Free, deterministic options day-trade screener. Runs twice daily via GitHub Actions,
producing up to 2 high-conviction trade ideas plus a lower-conviction table, per-pick
gamma/vanna heatmaps, and Discord alerts. Sources real exchange greeks/OI/IV from the
free CBOE delayed-quotes feed, falling back to `yfinance` when CBOE is unavailable.

## How it works

| Run | Schedule (ET) | Command |
| --- | --- | --- |
| Close | Mon–Fri 4:05 PM | `python -m src.daily_options_agent --mode=close` |
| Morning | Mon–Fri 9:10 AM | `python -m src.daily_options_agent --mode=morning` |

Pipeline: pull option chains (CBOE → yfinance fallback) → compute per-contract
Black-Scholes **gamma, vanna and charm** and aggregate them into dealer-signed
exposure grids (GEX / VEX / CEX) → derive key levels (gamma flip, vanna flip,
call/put walls) → score each contract → select trades → render heatmaps → post to
Discord.

### Dealer-positioning math

Greeks are computed analytically (normal pdf/cdf via `math.erf`, **no scipy**) and
verified against finite-difference of the Black-Scholes delta to ~1e-8:

- **GEX** = γ·OI·100·S²·sign — dealer delta shift per unit spot move.
- **VEX** = vanna·OI·100·S·sign — dealer delta shift per unit change in IV.
- **CEX** = charm·OI·100·S·sign — dealer delta shift per unit time (decay).

Dealer sign is +1 for calls, −1 for puts (long-calls / short-puts convention, so
positive net GEX ⇒ stabilising regime). The **flip** levels are the strikes where
cumulative exposure crosses zero, searched within ±25% of spot so deep-OTM noise on
0–2 DTE chains can't drag the flip into the wings. Net **vanna** sign drives the
strategy guidance: positive net vanna means an IV drop forces dealer **buying** (price
support); negative means dealer selling.

### Discord output (3 messages)

1. High-conviction pick #1 — strategy bullets + its own GEX/VEX heatmap.
2. High-conviction pick #2 — strategy bullets + its own GEX/VEX heatmap.
3. Lower-conviction table — ticker / type / strike / score / vol-OI.

## Project layout

```
.
├── .github/workflows/   # close-run.yml, morning-run.yml
├── src/
│   ├── daily_options_agent.py   # entrypoint / orchestration / Discord messages
│   ├── gex_calculator.py        # BS greeks, exposure grids, key levels, regimes
│   ├── cboe_source.py           # CBOE real-greeks chain fetch (yfinance fallback)
│   ├── scorer.py                # composite score (incl. real vanna/charm component)
│   ├── strategy_generator.py    # entry/target/stop + vanna/charm bullets
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

## Data source

CBOE delayed quotes (`cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json`)
provide real open interest, implied vol and exchange greeks for free. The screener uses
them when reachable and transparently falls back to `yfinance` otherwise; each
high-conviction alert shows which source it used. All logic is deterministic — no LLM.