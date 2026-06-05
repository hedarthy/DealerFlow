# Options Day-Trade Screener

Free, deterministic options day-trade screener. Runs twice daily via GitHub Actions,
producing up to 2 high-conviction trade ideas plus an additional-candidates table,
per-pick gamma/vanna heatmaps, and Discord alerts. Sources real exchange greeks/OI/IV
from the free CBOE delayed-quotes feed (falling back to `yfinance`), and layers an 8/21
EMA price-action filter on top so high-conviction picks must also be trending.

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

- **GEX** = γ·OI·100·S²·sign·0.01 — dealer-delta $ shift per **1% spot move**.
- **VEX** = vanna·OI·100·S·sign·0.01 — dealer-delta $ shift per **1 vol-point** of IV.
- **CEX** = charm·OI·100·S·sign / 365.25 — dealer-delta $ shift per **calendar day**.

Dealer sign is +1 for calls, −1 for puts (long-calls / short-puts convention, so
positive net GEX ⇒ stabilising regime). The **flip** levels are the strikes where
cumulative exposure crosses zero, searched within ±25% of spot so deep-OTM noise on
0–2 DTE chains can't drag the flip into the wings. Net **vanna** sign drives the
strategy guidance: positive net vanna means an IV drop forces dealer **buying** (price
support); negative means dealer selling.

### Scoring

Each contract gets a 0–100 composite from five **continuous** components — GEX regime
(`tanh` of the net dealer-gamma balance), order-flow (smooth vol/OI saturation),
liquidity (log-OI), vanna/charm concentration at the strike, and expected-move-scaled
moneyness — so liquid names spread across the range instead of piling onto one plateau.
The dominant weighted component is surfaced as the pick's **Edge**.

Two **directional confirmation overlays** sit on top of that (otherwise side-symmetric)
base score, because gamma/vanna/charm are nearly identical for a call and a put on the
same strike — without them the call-vs-put choice is barely better than a coin flip:

- **Dealer structure (GEX).** In a *positive-gamma* (mean-reverting) book the gamma flip
  is real support and the walls are real magnets, so a call sitting above the flip with
  room up to the call wall is confirmed (and a put below the flip with room to the put
  wall); chasing *past* a wall or fighting the flip is penalised; sitting *at* a wall or
  in the flip's reclaim zone is neutral. In a *negative-gamma* (trending) book the walls
  are weak, so this overlay abstains and defers to momentum — it never fades a breakout.
  Bands scale with the expected move. Toggle via `enable_gex_directional_filter`.
- **Price action (SeanTrades-style 8/21 EMA stack)**, below.

A pick reaches **high conviction** only if it clears the score cutoff, is **reachable**
(near enough the money for its expected move — `min_moneyness_high_conv`, which keeps
un-hittable far-OTM lottery strikes that top the board on raw vanna concentration out of
the top two), fights **neither** overlay, and is **confirmed by at least one**.

> All time-sensitive math (time-to-expiry → near-expiry gamma, the DTE window, the 0-DTE
> "past today's close" cutoff, the report date) is measured in **US/Eastern market time**,
> so the UTC CI runners don't understate time-to-close and distort 0–2 DTE greeks.

### Price-action confirmation (SeanTrades-style)

On top of the dealer-greek score, an **8/21 EMA stack** filter (computed from yfinance
daily closes) confirms momentum: a bullish stack (spot > 8EMA > 21EMA) confirms calls
and a bearish stack confirms puts. Confirmation adds points; a counter-trend contract is
docked and **barred from the two high-conviction picks** — so the top ideas must agree
with both dealer positioning *and* price action. Toggle via `enable_price_action_filter`
in `config.json`. Price-action layer inspired by [@SRxTrades](https://x.com/SRxTrades)'
swing methodology for higher-probability entries.

### Discord output (3 messages)

1. High-conviction pick #1 — regime + price-action **bias** + dealer-**structure** read,
   a plain-English regime note, strategy bullets (incl. EMA + structure confirmation),
   and its own GEX/VEX heatmap.
2. High-conviction pick #2 — same, for a **different** ticker (picks are deduped by
   underlying so #1 and #2 are never the same name).
3. Additional candidates — table of ticker / type / strike / **OTM%** / DTE / score /
   vol-OI / edge.

## Project layout

```
.
├── .github/workflows/   # close-run.yml, morning-run.yml
├── src/
│   ├── daily_options_agent.py   # entrypoint / orchestration / Discord messages
│   ├── gex_calculator.py        # BS greeks, exposure grids, key levels, regimes
│   ├── cboe_source.py           # CBOE real-greeks chain fetch (yfinance fallback)
│   ├── scorer.py                # composite score + EMA & GEX-structure overlays
│   ├── strategy_generator.py    # entry/target/stop + vanna/charm + confirmation bullets
│   ├── timeutil.py              # US/Eastern market-clock helper (UTC-runner safe)
│   ├── market_calendar.py       # NYSE trading-day calendar + schedule gate (stdlib only)
│   └── utils.py                 # persistence + Discord
├── tests/
│   ├── test_analysis.py         # offline overlay/scoring unit tests (no network)
│   └── test_schedule.py         # offline NYSE-calendar + cron-gate unit tests
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

## Automation & scheduling

Both runs are driven by GitHub Actions cron. Because GitHub cron is always in
UTC, each workflow ships **two** cron entries — one correct for EDT (summer) and
one for EST (winter) — and the agent gates itself to a single intended ET hour
so exactly one of them does work each day, year-round:

| Run | Intended (ET) | UTC crons (EDT / EST) |
| --- | --- | --- |
| Morning | 09:10 | `10 13 * * 1-5` / `10 14 * * 1-5` |
| Close | 16:05 | `5 20 * * 1-5` / `5 21 * * 1-5` |

The gate (`src/market_calendar.py` + the guard in `main()`):

- **Skips non-trading days** — weekends and NYSE holidays (`is_trading_day`).
- **Keys off the cron's *scheduled* ET hour, not the runner's start time** — so
  the off-season (wrong-DST) cron always self-skips even if GitHub delays it into
  the intended hour (no double-post), and the correct cron still owns the day even
  if it starts late (no silent miss).
- `--force` bypasses the gate. Manual `workflow_dispatch` passes `--force` by
  default; scheduled runs do not.

Reliability extras: a failed Discord post (or a missing webhook secret in CI)
now fails the job instead of succeeding silently, and each workflow has an
`if: failure()` step that pings Discord with the failed run's URL.

> **Note on GitHub's scheduler.** Scheduled workflows are best-effort — GitHub
> can delay or drop runs, and the *first* scheduled run of a newly-added cron is
> the most likely to be skipped. The dual-cron + gate makes runs correct and
> de-duplicated, but for hard guarantees trigger `workflow_dispatch` from an
> external always-on scheduler.

Offline tests (no network, stdlib only):

```bash
python tests/test_analysis.py     # scoring / overlay logic
python tests/test_schedule.py     # NYSE calendar + cron gate
```

## Configuration

Edit `config.json`:

- `watchlist` — tickers to scan.
- `dte_max` — max days to expiration considered.
- `min_volume`, `min_premium_k` — liquidity filters.
- `high_conviction_cutoff` — score threshold for the top bucket.
- `min_moneyness_high_conv` — reachability floor (0–100) for the top bucket; keeps
  far-OTM lottery strikes out of the two high-conviction picks.
- `enable_price_action_filter` — toggle the 8/21 EMA confirmation/trend gate.
- `enable_gex_directional_filter` — toggle the dealer-structure (flip/wall) directional gate.
- `score_weights` — weights for GEX regime, flow proxy, squeeze, vanna/charm, moneyness.

## Data sources

CBOE delayed quotes (`cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json`)
provide real open interest, implied vol and exchange greeks for free, and are the source
of truth for the option chain — they're used when reachable, with a transparent
`yfinance` fallback otherwise (each high-conviction alert shows which source it used).
The two feeds are **not merged**: they're independent snapshots, so summing or averaging
their open interest would corrupt the dealer-positioning signal.

`yfinance` is used separately for what CBOE's quote feed lacks — the underlying's **daily
price history**, which powers the 8/21 EMA price-action stack. That same history also
cross-checks the live CBOE spot and flags a stale quote if they diverge sharply. A
per-run source-coverage summary is printed (e.g. `cboe=52`). All logic is deterministic —
no LLM.