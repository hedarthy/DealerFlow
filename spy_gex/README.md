# SPY Dealerflow GEX Heatmap Alert (`spy_gex/`)

A **standalone, self-contained** hourly alert that posts SPY dealer-positioning
**gamma / vanna / charm** heatmaps to Discord, so you can see where dealer flow
magnetises price (the gamma flip and the call/put walls) across the front of the curve.

It is deliberately isolated from the repo's twice-daily watchlist screener in `src/`:
this package **vendors its own copies** of the Black-Scholes exposure math, the
CBOE→yfinance chain fetch, the NYSE calendar and the Discord poster. Nothing here
imports from `src/`, and nothing in `src/` imports from here — so this alert can run on
its own and can never break (or be broken by) the other pipeline. It also ignores the
screener's SeanTrades 8/21-EMA price-action layer entirely.

## What each run produces

For SPY it renders **three separate heatmaps**, each a **strike (rows) × expiration-date
(columns)** grid for the **five nearest expiries** (0DTE today through ~4 sessions out),
windowed to **25 strikes above and below spot**:

| Image | Greek | Reads as |
|-------|-------|----------|
| `spy_gex_summary.png` | **Magnet table** | Per-expiry regime, walls, net Σ greeks |
| `spy_gex_gamma.png` | **GEX** (gamma) | Pin/resistance vs. acceleration zones |
| `spy_gex_vanna.png` | **VEX** (vanna) | Where IV moves push dealer hedging |
| `spy_gex_charm.png` | **CEX** (charm) | Delta decay drift into expiry |
| `spy_gex_front_triptych.png` | **Front expiry** | Gamma · Vanna · Charm side by side for the nearest expiry |

Styling matches the Skylit-AI look: dark background, **viridis** (min-max) colour scale,
a **white dashed line + tag at spot**, dates across the top, and the largest-magnitude
**"King" strike starred (★)**. A short **header** (spot, regime, gamma flip, call/put walls,
plain-English read) plus a **magnet-table card** (`spy_gex_summary.png` — per-expiry regime,
walls and net GEX/VEX/CEX, sign-coloured, rendered as an image so it never wraps) is posted
first, then the three heatmaps, and finally a **front-expiry triptych** (`spy_gex_front_triptych.png`
— the nearest expiry's gamma/vanna/charm side by side, each on its own colour scale, sharing
the strike axis) — **five Discord messages per run**.

## Schedule (ET, NYSE trading days only)

9:31 (one minute after the open), then on the hour: 10:00, 11:00, 12:00, 13:00, 14:00,
15:00, 16:00. Half-day early closes suppress the post-1:00 PM slots.

Robustness: dual EST/EDT UTC crons are deduped by a DST-correct **scheduled-ET-time slot
gate** (each UTC cron maps to exactly one ET time per date, so the off-DST one self-skips
— no double-post), with a freshness cap (20 min intraday, 45 min for the close) guarding
against GitHub scheduler latency.

## Run it

```bash
# From the repo root. --force bypasses the schedule gate.
python -m spy_gex.agent --force
```

With no `DISCORD_WEBHOOK_URL` set it renders the images + `spy_gex_report.md` into this
folder and posts nothing (safe local dry run). Artifacts (`spy_gex_*.png`,
`spy_gex_report.md`) are git-ignored.

## Discord webhook

The workflow resolves, in order: `SPY_BOY_DISCORD_WEBHOOK` →
`SPY_GEX_DISCORD_WEBHOOK_URL` → `OPTIONS_DISCORD_WEBHOOK_URL`, and maps the first one set
into `DISCORD_WEBHOOK_URL` for the run. `SPY_BOY_DISCORD_WEBHOOK` is the dedicated SPY
dealerflow channel, so this alert posts there without touching the screener's webhook.

## Tests

```bash
python -m spy_gex.tests.test_spy_gex
```

Offline (no network): strike window, gamma flip, dealer-signed exposure, the schedule
slot dedup (including DST-switch weeks), early-close suppression, and `slot_decision`.

## Layout

```
spy_gex/
  agent.py          # entrypoint: schedule gate, render, post
  exposure.py       # vendored BS gamma/vanna/charm + exposure grids + key levels
  data_source.py    # vendored CBOE → yfinance chain fetch
  calendar_util.py  # vendored NYSE calendar + Eastern-time + slot helpers
  notify.py         # vendored Discord poster
  tests/            # offline tests
```

Scheduling lives in `.github/workflows/spy-gex-run.yml`.
