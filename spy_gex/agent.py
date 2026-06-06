"""Hourly SPY dealer-flow alert: gamma / vanna / charm GEX heatmaps to Discord.

A standalone, self-contained alert. It does not touch — and does not import from —
the twice-daily watchlist screener in ``src/`` (and deliberately ignores that
pipeline's SeanTrades 8/21-EMA price-action layer). For SPY it pulls the option chain
(CBOE real exchange OI/IV first, ``yfinance`` fallback), computes the dealer-signed
gamma (GEX), vanna (VEX) and charm (CEX) exposure grids per expiration, and renders
one Skylit-style heatmap per greek — strike rows by expiration-date columns — so a
trader can see where dealer positioning magnetises price across the curve (the gamma
flip and the call/put walls). A white line + tag marks spot; the largest-magnitude
"King" strike is starred.

It runs every NYSE trading day at one minute after the open (9:31 ET) and then on the
hour through the close (10:00 .. 16:00 ET). Expirations rendered: the five nearest SPY
expiries on/after the run date (0DTE today through ~four sessions out). Each heatmap
windows to the 25 strikes above and 25 at/below spot.

Run:  ``python -m spy_gex.agent [--force]``  (``--force`` bypasses the schedule gate; a
local run with no webhook renders the images and prints, posting nothing).
"""
import argparse
import os
import time
from datetime import datetime

from dotenv import load_dotenv
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

from spy_gex.exposure import (
    compute_exposure_grids, get_key_levels, get_regime, get_vanna_regime,
    select_window_strikes,
)
from spy_gex.data_source import get_chains
from spy_gex.notify import send_discord
from spy_gex.calendar_util import (
    eastern_now, is_trading_day, cron_scheduled_et_time, market_close_hm, SPY_GEX_SLOTS,
)

# Resolve artifacts inside this package and load the shared repo-root .env, without
# changing the process working directory (so we never disturb other tools).
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(PKG_DIR)
load_dotenv(os.path.join(REPO_ROOT, ".env"))


def _art(name):
    """Absolute path to an output artifact, kept inside the package folder."""
    return os.path.join(PKG_DIR, name)


TICKER = "SPY"
N_EXPIRIES = 5          # current day + ~4 sessions out
WINDOW_STRIKES = 25     # strikes shown above and below spot
SLOT_FRESHNESS_MIN = 20  # skip an intraday slot whose run starts more than this many minutes late
# The day's final (close) slot has no later run to supersede it, so allow a more generous
# delay window before dropping it — a moderately late close snapshot is still worth posting.
CLOSE_FRESHNESS_MIN = 45

# Skylit-AI-style dark heatmap palette (viridis on near-black, white text/spot tag).
SKY_BG = "#0b0d10"        # figure / axes background
SKY_TEXT = "#e6e8eb"      # ticks, labels, titles
SKY_GRID = "#1b1f24"      # cell separators / colorbar outline
SKY_SPOT = "#ffffff"      # spot line + tag
SKY_CMAP = "viridis"      # min-max normalised (NOT centred at 0), matching Skylit
SKY_KING = "★"            # marks the largest-magnitude (King) strike


# --------------------------------------------------------------------------- data

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

def build_greek_matrix(per_exp, window, exp_labels, divisor=1e6):
    """Strike-rows (descending) × expiration-date-columns matrix in $M.

    ``per_exp`` maps an expiry label -> that expiry's per-strike greek dict. ``window``
    is the shared strike axis (one set of rows for every column). A strike absent for an
    expiry becomes ``NaN`` (rendered as a dark gap), distinct from a present strike whose
    exposure happens to net to ~0 (rendered in-scale).
    """
    strikes = sorted(window, reverse=True)
    data = {
        label: [
            (per_exp[label][k] / divisor if (label in per_exp and k in per_exp[label])
             else np.nan)
            for k in strikes
        ]
        for label in exp_labels
    }
    return pd.DataFrame(data, index=strikes, columns=exp_labels)


def _annot_grid(mat, decimals):
    """String annotations: blank missing/near-zero cells, star the single King (max |value|)."""
    arr = mat.to_numpy(dtype=float)
    out = np.empty(arr.shape, dtype=object)
    if arr.size == 0:
        return out
    absarr = np.abs(arr)
    has_value = np.isfinite(absarr).any()
    peak = float(np.nanmax(absarr)) if has_value else 0.0
    floor = 0.005 * peak  # hide cells under 0.5% of the King to cut clutter
    king = np.unravel_index(np.nanargmax(absarr), arr.shape) if peak > 0 else None
    for i in range(arr.shape[0]):
        for j in range(arr.shape[1]):
            v = arr[i, j]
            if not np.isfinite(v) or peak == 0 or abs(v) < floor:
                out[i, j] = SKY_KING if king == (i, j) else ""
            else:
                out[i, j] = f"{v:,.{decimals}f}" + (SKY_KING if king == (i, j) else "")
    return out


def _style_dark(fig, ax, cbar):
    """Apply the Skylit dark theme to a heatmap axis + its colorbar."""
    fig.patch.set_facecolor(SKY_BG)
    ax.set_facecolor(SKY_BG)
    ax.tick_params(colors=SKY_TEXT, labelsize=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    if cbar is not None:
        cbar.outline.set_edgecolor(SKY_GRID)
        cbar.ax.yaxis.set_tick_params(color=SKY_TEXT, labelcolor=SKY_TEXT)
        cbar.ax.yaxis.label.set_color(SKY_TEXT)


def render_grid(mat, spot, title, cbar_label, path, decimals=1):
    """Render one greek as a Skylit-style strike×expiry heatmap and save to ``path``.

    Dark background, viridis (min-max) colour scale, a white dashed spot line + tag,
    dates across the top, and the King strike starred.
    """
    strikes = list(mat.index)
    nrows, ncols = mat.shape
    height = max(9.0, 0.30 * nrows + 1.6)
    width = max(7.5, 2.0 + 1.5 * ncols)
    fig, ax = plt.subplots(figsize=(width, height))
    annot = _annot_grid(mat, decimals)
    mask = ~np.isfinite(mat.to_numpy(dtype=float))  # strikes absent for an expiry -> dark gap
    sns.heatmap(
        mat, ax=ax, cmap=SKY_CMAP, annot=annot, fmt="", mask=mask,
        annot_kws={"size": 6, "color": SKY_TEXT},
        linewidths=0.4, linecolor=SKY_GRID,
        cbar_kws={"label": cbar_label, "shrink": 0.6, "pad": 0.02},
    )
    cbar = ax.collections[0].colorbar if ax.collections else None
    _style_dark(fig, ax, cbar)

    ax.xaxis.tick_top()
    ax.xaxis.set_label_position("top")
    ax.set_xticklabels(mat.columns, rotation=0, color=SKY_TEXT, fontsize=8)
    ax.set_yticklabels(strikes, rotation=0, color=SKY_TEXT, fontsize=7)
    ax.set_ylabel("")

    # Spot line + tag: rows run high→low, so the boundary sits below every strike > spot.
    n_above = sum(1 for k in strikes if k > spot)
    ax.axhline(n_above, color=SKY_SPOT, lw=1.6, ls=(0, (5, 2)), zorder=5)
    ax.annotate(
        f"spot ${spot:.2f}", xy=(0, n_above), xycoords=("axes fraction", "data"),
        xytext=(-8, 0), textcoords="offset points", ha="right", va="center",
        color=SKY_BG, fontsize=7.5, fontweight="bold", clip_on=False, zorder=6,
        bbox=dict(boxstyle="round,pad=0.3", fc=SKY_SPOT, ec="none"),
    )

    fig.suptitle(title, color=SKY_TEXT, fontsize=12, fontweight="bold")
    plt.savefig(path, dpi=120, facecolor=SKY_BG, bbox_inches="tight", pad_inches=0.35)
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

    rows = []
    per_exp = {"gex": {}, "vex": {}, "cex": {}}
    exp_labels = []
    all_strikes = set()
    for exp, dte in expiries:
        df = chains[exp]
        gex, vex, cex = compute_exposure_grids(df, spot, exp)
        if not gex:
            print(f"  {exp}: no usable open interest; skipping")
            continue
        keys = get_key_levels(gex, spot)
        regime = get_regime(gex)
        vanna_regime = get_vanna_regime(vex)
        col = f"{exp[5:]}\nD{dte}"   # column header: MM-DD over its DTE
        exp_labels.append(col)
        per_exp["gex"][col] = gex
        per_exp["vex"][col] = vex
        per_exp["cex"][col] = cex
        all_strikes.update(gex.keys())
        rows.append({
            "exp": exp, "dte": dte, "regime": regime, "vanna_regime": vanna_regime,
            "keys": keys,
            "flip_s": (f"${keys['gamma_flip']:.0f}" if keys["gamma_flip"] else "n/a"),
            "cw_s": (f"${keys['call_wall']:.0f}" if keys["call_wall"] else "n/a"),
            "pw_s": (f"${keys['put_wall']:.0f}" if keys["put_wall"] else "n/a"),
            "net_gex": sum(gex.values()) / 1e6,
            "net_vex": sum(vex.values()) / 1e6,
            "net_cex": sum(cex.values()) / 1e6,
        })

    if not rows:
        print("No SPY expirations with open interest; nothing to render.")
        _post(f"# 🧲 SPY Dealerflow — {label}\n\n_No SPY expirations with open interest right now._")
        return

    window = select_window_strikes(all_strikes, spot, WINDOW_STRIKES)
    dates = " / ".join(r["exp"] for r in rows)
    base = f"SPY · spot ${spot:.2f} · {label} · {dates}"

    grids_meta = [
        ("gex", "Gamma (GEX)", "$M per 1% spot", "spy_gex_gamma.png", 1,
         "🟢 **SPY Gamma (GEX)** — dealer gamma by strike × expiry. Positive (bright) "
         "rows are call-heavy pin/resistance magnets; negative (dark) rows accelerate "
         "moves. The King ★ is the dominant strike on the board."),
        ("vex", "Vanna (VEX)", "$M per vol-pt", "spy_gex_vanna.png", 1,
         "🟣 **SPY Vanna (VEX)** — how dealer hedging shifts when IV moves. Bright rows "
         "draw price on a vol drop / supportive flows; dark rows pressure price as vol "
         "rises. King ★ = largest vanna magnet."),
        ("cex", "Charm (CEX)", "$M per day", "spy_gex_charm.png", 2,
         "🟠 **SPY Charm (CEX)** — delta decay into expiry (time-of-day drift). Bright "
         "rows pull price up as charm hedging buys; dark rows bleed it lower. Strongest "
         "near expiry. King ★ = dominant charm strike."),
    ]
    images = []
    for key, name, unit, fname, dec, caption in grids_meta:
        mat = build_greek_matrix(per_exp[key], window, exp_labels)
        path = _art(fname)
        render_grid(mat, spot, f"{name} — {base}", unit, path, decimals=dec)
        images.append((caption, path))

    summary = build_summary(spot, source, slot, et, rows)
    _post(summary)
    time.sleep(1)
    for caption, png in images:
        _post(caption, png)
        time.sleep(1)

    with open(_art("spy_gex_report.md"), "w") as f:
        f.write(summary + "\n\n" + "\n\n".join(c for c, _ in images))
    print(f"✅ SPY GEX alert posted — {len(rows)} expiries × 3 greek grids, source {source}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hourly SPY dealer-flow GEX heatmap alert")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the trading-day / schedule-slot gate (manual / local runs).")
    args = parser.parse_args()
    main(force=args.force)
