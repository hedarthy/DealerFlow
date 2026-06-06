"""Hourly SPY dealer-flow alert: gamma / vanna / charm GEX heatmaps to Discord.

A standalone, additive alert (it does not touch the twice-daily watchlist screener
in ``daily_options_agent.py`` and deliberately ignores that pipeline's SeanTrades
8/21-EMA price-action layer). For SPY it pulls the option chain (CBOE real exchange
OI/IV first, ``yfinance`` fallback), computes the dealer-signed gamma (GEX), vanna
(VEX) and charm (CEX) exposure grids per expiration, and renders one three-panel
heatmap per expiration so a trader can see where dealer positioning magnetises price
— the gamma flip and the call/put walls.

It runs every NYSE trading day at one minute after the open (9:31 ET) and then on the
hour through the close (10:00 .. 16:00 ET). Expirations rendered: the five nearest SPY
expiries on/after the run date (0DTE today through ~four sessions out). Each heatmap
windows to the 25 strikes above and 25 at/below spot.

Run:  ``python -m src.spy_gex_agent [--force]``  (``--force`` bypasses the schedule
gate; a local run with no webhook renders the images and prints, posting nothing).
"""
import argparse
import os
import time
from datetime import datetime

from dotenv import load_dotenv
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

from src.gex_calculator import (
    compute_exposure_grids, get_key_levels, get_regime, get_vanna_regime,
    select_window_strikes,
)
from src.cboe_source import fetch_cboe
from src.utils import send_discord
from src.timeutil import eastern_now
from src.market_calendar import (
    is_trading_day, cron_scheduled_et_time, market_close_hm, SPY_GEX_SLOTS,
)

# Resolve relative paths (.env, artifacts) from the repo root regardless of the
# caller's working directory, matching daily_options_agent.
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

TICKER = "SPY"
N_EXPIRIES = 5          # current day + ~4 sessions out
WINDOW_STRIKES = 25     # strikes shown above and below spot
SLOT_FRESHNESS_MIN = 20  # skip an intraday slot whose run starts more than this many minutes late
# The day's final (close) slot has no later run to supersede it, so allow a more generous
# delay window before dropping it — a moderately late close snapshot is still worth posting.
CLOSE_FRESHNESS_MIN = 45


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # filter NaN


# --------------------------------------------------------------------------- data

def get_chains(ticker):
    """Return ``(spot, {expiry: df}, source)`` for ``ticker`` or ``None``.

    Prefer CBOE (real exchange OI/IV) and fall back to yfinance so a CBOE outage
    never silences the alert. Both sources yield DataFrames with the same columns
    (strike, opt_type, openInterest, impliedVolatility, volume, lastPrice,
    contractSymbol), so downstream processing is source-agnostic. CBOE and yfinance
    are never merged — they are independent snapshots.
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
    for exp_str in (stock.options or [])[:8]:
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


def select_expiries(chains, effective_now):
    """The ``N_EXPIRIES`` nearest expiries on/after the run date.

    "Nearest five on/after today" rather than a fixed calendar-day cap, so weekends
    and holidays can't silently drop a valid near expiry. Today's 0DTE is included
    only while at/before the session close — keyed off ``effective_now`` (the
    scheduled slot time) so the decision is deterministic per slot, not subject to
    GitHub runner jitter.
    """
    today = effective_now.date()
    close_hm = market_close_hm(today)
    past_close = (effective_now.hour, effective_now.minute) > close_hm
    dated = []
    for exp in chains:
        try:
            d = datetime.strptime(exp, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if d < today or (d == today and past_close):
            continue
        dated.append((d, exp))
    dated.sort()
    return [(exp, (d - today).days) for d, exp in dated[:N_EXPIRIES]]


# --------------------------------------------------------------------------- render

def _panel(grid, window):
    """A single-column, descending-strike DataFrame (in $M) for one greek grid."""
    rows = [(k, grid.get(k, 0.0) / 1e6) for k in sorted(window, reverse=True)]
    return pd.DataFrame(rows, columns=["strike", "val"]).set_index("strike")[["val"]]


def render_heatmap(spot, exp, dte, grids, keys, path):
    """Three-panel gamma/vanna/charm dealer-exposure heatmap for one expiration."""
    gex, vex, cex = grids
    window = select_window_strikes(gex.keys(), spot, WINDOW_STRIKES)
    g = _panel(gex, window).rename(columns={"val": "GEX"})
    v = _panel(vex, window).rename(columns={"val": "VEX"})
    c = _panel(cex, window).rename(columns={"val": "CEX"})

    height = max(8.0, 0.30 * len(window) + 2.0)
    fig, axes = plt.subplots(1, 3, figsize=(15, height))
    ann = {"size": 6}
    sns.heatmap(g, annot=True, fmt=".1f", cmap="RdYlGn", center=0, ax=axes[0],
                annot_kws=ann, cbar_kws={"label": "$M / 1% spot"})
    axes[0].set_title("Gamma (GEX)")
    axes[0].set_xlabel("")
    axes[0].set_ylabel("Strike")
    sns.heatmap(v, annot=True, fmt=".1f", cmap="PuOr", center=0, ax=axes[1],
                annot_kws=ann, cbar_kws={"label": "$M / vol-pt"}, yticklabels=False)
    axes[1].set_title("Vanna (VEX)")
    axes[1].set_xlabel("")
    axes[1].set_ylabel("")
    sns.heatmap(c, annot=True, fmt=".2f", cmap="BrBG", center=0, ax=axes[2],
                annot_kws=ann, cbar_kws={"label": "$M / day"}, yticklabels=False)
    axes[2].set_title("Charm (CEX)")
    axes[2].set_xlabel("")
    axes[2].set_ylabel("")

    def lvl(x):
        return f"${x:.0f}" if x else "n/a"

    fig.suptitle(
        f"SPY {exp} (DTE {dte}) — spot ${spot:.2f} | γflip {lvl(keys['gamma_flip'])} · "
        f"call wall {lvl(keys['call_wall'])} · put wall {lvl(keys['put_wall'])}")
    plt.tight_layout()
    plt.savefig(path, dpi=110)
    plt.close(fig)


# --------------------------------------------------------------------------- text

def _rel(x, spot):
    if not x:
        return "n/a"
    if not spot:
        return f"${x:.2f}"
    return f"${x:.0f} ({(x / spot - 1) * 100:+.1f}%)"


def magnet_read(spot, keys, regime):
    """One-line plain-English read of where dealer flow magnetises price."""
    flip, cw, pw = keys["gamma_flip"], keys["call_wall"], keys["put_wall"]
    if regime == "positive":
        out = ["**Positive γ** — dealers fade moves; expect pinning / mean-reversion "
               "between the walls."]
    else:
        out = ["**Negative γ** — dealers chase; expect trending / breakouts, walls are weak."]
    if flip:
        side = "above" if spot >= flip else "below"
        out.append(f"Spot ${spot:.2f} is {side} the γ-flip {_rel(flip, spot)}.")
    if cw:
        out.append(f"Call-wall magnet {_rel(cw, spot)}.")
    if pw:
        out.append(f"Put-wall magnet {_rel(pw, spot)}.")
    return " ".join(out)


def build_summary(spot, source, slot, et, rows):
    """Header + per-expiry magnet table. ``rows`` is a list of dicts per expiry."""
    when = f"{slot[0]:02d}:{slot[1]:02d} ET slot" if slot else f"{et:%H:%M} ET (forced)"
    msg = "# 🧲 SPY Dealerflow — Gamma · Vanna · Charm Magnet Map\n"
    msg += f"**{et:%a %b %d %Y} · {when}** · spot **${spot:.2f}** · source `{source}` " \
           f"(data as of {et:%H:%M} ET)\n"
    if rows:
        msg += magnet_read(spot, rows[0]["keys"], rows[0]["regime"]) + "\n"
        header = (f"{'Exp':<12}{'DTE':>4}{'Reg':>5}{'Flip':>8}{'CallWall':>10}"
                  f"{'PutWall':>9}{'ΣGEX':>9}{'ΣVanna':>9}{'ΣCharm':>9}")
        lines = [header, "-" * len(header)]
        for r in rows:
            k = r["keys"]
            lines.append(
                f"{r['exp']:<12}{r['dte']:>4}{('+γ' if r['regime'] == 'positive' else '-γ'):>5}"
                f"{r['flip_s']:>8}{r['cw_s']:>10}{r['pw_s']:>9}"
                f"{r['net_gex']:>9.0f}{r['net_vex']:>9.1f}{r['net_cex']:>9.2f}")
        msg += "```\n" + "\n".join(lines) + "\n```"
        msg += "_ΣGEX $M per 1% spot · ΣVanna $M per vol-pt · ΣCharm $M per day · " \
               "walls = price magnets._"
    else:
        msg += "_No usable SPY expirations with open interest right now._"
    return msg


# --------------------------------------------------------------------------- gate

def _freshness_allowance(slot, close_hm):
    """Minutes a slot may start late before it's dropped. The day's final (close) slot
    gets a wider window since no later run will cover it; intraday slots stay tight so a
    badly delayed run can't post stale, overlapping with the next hourly."""
    return CLOSE_FRESHNESS_MIN if tuple(slot) == tuple(close_hm) else SLOT_FRESHNESS_MIN


def slot_decision(et, force, cron):
    """Return ``(action, slot_or_reason)``.

    ``action`` is ``"run"`` (with the resolved ``(hour, minute)`` slot), ``"force"``
    (bypass; slot is ``None``), or ``"skip"`` (with a human reason). The schedule is
    deduped across the dual EST/EDT crons by keying on the cron's *scheduled* ET time
    (DST-correct), and protected from GitHub scheduler latency by a freshness cap and
    an early-close cutoff.
    """
    if force:
        return "force", None
    if not is_trading_day(et.date()):
        return "skip", f"{et.date()} ({et:%A}) is not an NYSE trading day"
    close_hm = market_close_hm(et.date())
    if cron:
        sched = cron_scheduled_et_time(cron, et.date())
        if sched is None or tuple(sched) not in SPY_GEX_SLOTS:
            return "skip", (f"cron '{cron}' is scheduled for ET {sched} — not a SPY alert "
                            f"slot (off-DST duplicate or non-slot minute)")
        if tuple(sched) > close_hm:
            return "skip", (f"slot {sched[0]:02d}:{sched[1]:02d} ET is after the "
                            f"{close_hm[0]:02d}:{close_hm[1]:02d} early close today")
        sched_dt = datetime(et.year, et.month, et.day, sched[0], sched[1])
        delay = (et - sched_dt).total_seconds() / 60.0
        allow = _freshness_allowance(sched, close_hm)
        if delay > allow:
            return "skip", (f"slot {sched[0]:02d}:{sched[1]:02d} ET started {delay:.0f} min "
                            f"late (> {allow:.0f}); the next run will cover it")
        return "run", tuple(sched)
    # No cron context (a manual, non-forced run): match the wall clock to a slot.
    near = min(SPY_GEX_SLOTS,
               key=lambda s: abs((et - datetime(et.year, et.month, et.day, s[0], s[1])).total_seconds()))
    if near > close_hm:
        return "skip", f"{et:%H:%M} ET is past today's {close_hm[0]:02d}:{close_hm[1]:02d} close"
    near_dt = datetime(et.year, et.month, et.day, near[0], near[1])
    if abs((et - near_dt).total_seconds()) / 60.0 <= _freshness_allowance(near, close_hm):
        return "run", near
    return "skip", (f"{et:%H:%M} ET is not within range of a SPY slot; "
                    f"use --force to run anyway")


# --------------------------------------------------------------------------- main

def _post(content, png=None, tries=3):
    """``send_discord`` with light retry/backoff (Discord 429 / transient 5xx)."""
    for i in range(tries):
        try:
            send_discord(content, png)
            return
        except Exception as e:
            if i == tries - 1:
                raise
            wait = 3 * (i + 1)
            print(f"⚠️  Discord post failed ({e}); retrying in {wait}s")
            time.sleep(wait)


def main(force=False):
    et = eastern_now()
    cron = os.getenv("SCHEDULED_CRON", "").strip()
    action, info = slot_decision(et, force, cron)
    if action == "skip":
        print(f"⏭️  SKIPPED: {info}")
        return
    slot = info  # (hour, minute) or None when forced
    effective_now = datetime(et.year, et.month, et.day, slot[0], slot[1]) if slot else et
    label = f"{slot[0]:02d}:{slot[1]:02d} ET" if slot else f"{et:%H:%M} ET (forced)"
    print(f"🚀 SPY GEX alert — {label} (now {et:%Y-%m-%d %H:%M} ET)")

    got = get_chains(TICKER)
    if not got:
        raise RuntimeError("Could not fetch SPY option chain from CBOE or yfinance")
    spot, chains, source = got

    expiries = select_expiries(chains, effective_now)
    if not expiries:
        print("No SPY expirations on/after today; nothing to render.")
        _post(f"# 🧲 SPY Dealerflow — {label}\n\n_No SPY expirations available right now._")
        return

    rows, images = [], []
    for idx, (exp, dte) in enumerate(expiries, 1):
        df = chains[exp]
        gex, vex, cex = compute_exposure_grids(df, spot, exp)
        if not gex:
            print(f"  {exp}: no usable open interest; skipping")
            continue
        keys = get_key_levels(gex, spot)
        regime = get_regime(gex)
        vanna_regime = get_vanna_regime(vex)
        net_gex = sum(gex.values()) / 1e6
        net_vex = sum(vex.values()) / 1e6
        net_cex = sum(cex.values()) / 1e6
        png = f"spy_gex_heatmap_{idx}.png"
        render_heatmap(spot, exp, dte, (gex, vex, cex), keys, png)
        rows.append({
            "exp": exp, "dte": dte, "regime": regime, "keys": keys,
            "flip_s": (f"${keys['gamma_flip']:.0f}" if keys["gamma_flip"] else "n/a"),
            "cw_s": (f"${keys['call_wall']:.0f}" if keys["call_wall"] else "n/a"),
            "pw_s": (f"${keys['put_wall']:.0f}" if keys["put_wall"] else "n/a"),
            "net_gex": net_gex, "net_vex": net_vex, "net_cex": net_cex,
        })
        caption = (f"**SPY {exp} · DTE {dte}** — {'+γ' if regime == 'positive' else '-γ'} · "
                   f"vanna {vanna_regime} · γflip {_rel(keys['gamma_flip'], spot)} · "
                   f"call wall {_rel(keys['call_wall'], spot)} · "
                   f"put wall {_rel(keys['put_wall'], spot)} · ΣGEX {net_gex:.0f}M/1%")
        images.append((caption, png))

    summary = build_summary(spot, source, slot, et, rows)
    _post(summary)
    time.sleep(1)
    for caption, png in images:
        _post(caption, png)
        time.sleep(1)

    with open("spy_gex_report.md", "w") as f:
        f.write(summary + "\n\n" + "\n\n".join(c for c, _ in images))
    print(f"✅ SPY GEX alert posted — {len(images)} expirations, source {source}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hourly SPY dealer-flow GEX heatmap alert")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the trading-day / schedule-slot gate (manual / local runs).")
    args = parser.parse_args()
    main(force=args.force)
