"""Render + text engine for the on-demand ticker dealerflow alert.

A generalized copy of the hourly SPY alert's rendering and summary code, parameterized
by ticker and stripped of the schedule gate / hard-coded symbol / file orchestration so
it can render any requested symbol on demand. Produces the same five-message construct:

1. a titled **summary magnet-table card** (``render_summary_table``) with sign-coloured
   ΣGEX / ΣVanna / ΣCharm columns, plus a markdown caption carrying the header and a
   plain-English "magnet read" (``build_summary_text`` + ``magnet_read``);
2-4. the **Gamma / Vanna / Charm** heatmaps (``render_grid``, strike rows x expiry cols);
5. the **front-expiry triptych** (``render_front_triptych``, nearest expiry's three
   greeks side by side with per-panel colorbars).

The Skylit-style dark look (viridis min-max, white dashed spot line + tag, King ★ on the
largest-magnitude strike, per-cell WCAG-contrast annotations) and the ``$X,XXX.XK``
formatting are preserved exactly.
"""
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from matplotlib.ticker import FuncFormatter
from mpl_toolkits.axes_grid1 import make_axes_locatable
import seaborn as sns

N_EXPIRIES = 5          # current day + ~4 sessions out
WINDOW_STRIKES = 25     # strikes shown above and below spot
MARKET_CLOSE_HM = (16, 0)  # regular NYSE close (ET); 0DTE drops after this on-demand

# Skylit-AI-style dark heatmap palette (viridis on near-black, white text/spot tag).
SKY_BG = "#0b0d10"        # figure / axes background
SKY_TEXT = "#e6e8eb"      # ticks, labels, titles
SKY_GRID = "#1b1f24"      # cell separators / colorbar outline
SKY_SPOT = "#ffffff"      # spot line + tag
SKY_CMAP = "viridis"      # min-max normalised (NOT centred at 0), matching Skylit
SKY_KING = "\u2605"       # marks the largest-magnitude (King) strike
SKY_POS = "#3fb950"       # positive $ / call-side (green)
SKY_NEG = "#f85149"       # negative $ / put-side (red)
SKY_ROW_ALT = "#12161c"   # subtle zebra stripe on the summary table
SKY_MUTED = "#9aa3ad"     # de-emphasised text (legend)

# Column legend shared by the summary table image and the local report artifact.
SUMMARY_LEGEND = ("\u03a3GEX $K per 1% spot   \u00b7   \u03a3Vanna $K per 1.00\u03c3   \u00b7   "
                  "\u03a3Charm $K per day   \u00b7   walls = price magnets")


def _fmt_k(v, decimals=1):
    """Format a $-thousands value Skylit-style: ``$1,234.5K`` / ``-$1,234.5K`` / ``$0.0K``.

    Inputs are already scaled to thousands of dollars (so ``$1,000.0K`` == $1,000,000).
    """
    sign = "-$" if v < 0 else "$"
    return f"{sign}{abs(v):,.{decimals}f}K"


# --------------------------------------------------------------------------- data

def select_expiries(chains, now_et, n=N_EXPIRIES):
    """The ``n`` nearest expiries on/after the run date.

    "Nearest n on/after today" rather than a fixed calendar-day cap, so weekends and
    holidays can't silently drop a valid near expiry. Today's 0DTE is included only
    while strictly before the regular 16:00 ET close — measured off ``now_et`` so the
    decision is deterministic for the request.

    Note: this uses the regular close time; on an NYSE early-close half-day a same-day
    expiry can still appear between 13:00 and 16:00 ET. That's an accepted edge for an
    on-demand tool (the underlying calendar gate lives only in the scheduled alert).
    """
    today = now_et.date()
    past_close = (now_et.hour, now_et.minute) >= MARKET_CLOSE_HM
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
    return [(exp, (d - today).days) for d, exp in dated[:n]]


# --------------------------------------------------------------------------- render

def build_greek_matrix(per_exp, window, exp_labels, divisor=1e3):
    """Strike-rows (descending) x expiration-date-columns matrix in $K (thousands).

    ``per_exp`` maps an expiry label -> that expiry's per-strike greek dict. ``window``
    is the shared strike axis (one set of rows for every column). A strike absent for an
    expiry becomes ``NaN`` (rendered as a dark gap), distinct from a present strike whose
    exposure happens to net to ~0 (rendered in-scale). Values are in thousands of dollars
    so they read as ``$1,234.5K`` (Skylit style).
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
                out[i, j] = _fmt_k(v, decimals) + (SKY_KING if king == (i, j) else "")
    return out


def _style_dark(fig, ax, cbar):
    """Apply the Skylit dark theme to a heatmap axis + its colorbar."""
    fig.patch.set_facecolor(SKY_BG)
    ax.set_facecolor(SKY_BG)
    ax.tick_params(colors=SKY_TEXT, labelsize=10)
    for spine in ax.spines.values():
        spine.set_visible(False)
    if cbar is not None:
        cbar.outline.set_edgecolor(SKY_GRID)
        cbar.ax.yaxis.set_tick_params(color=SKY_TEXT, labelcolor=SKY_TEXT)
        cbar.ax.yaxis.label.set_color(SKY_TEXT)


def _rel_luminance(rgb):
    """sRGB relative luminance (WCAG) of an (r,g,b) triple in 0..1."""
    r, g, b = (
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        for c in rgb[:3]
    )
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_annotations(ax, mat):
    """Recolour each cell's number for maximum contrast against *its own* viridis cell.

    Seaborn's default annotation colour is a fixed light tone, which is unreadable on the
    bright (yellow/green) end of viridis. Here every annotation is set to pure black or
    white based on the cell's luminance, with a thin opposite-colour outline so the digits
    stay legible even on mid-tone cells.
    """
    if not ax.collections:
        return
    mesh = ax.collections[0]
    values = mat.to_numpy(dtype=float)
    nrows, ncols = values.shape
    for t in ax.texts:
        x, y = t.get_position()          # seaborn places text at (col + .5, row + .5)
        j, i = int(round(x - 0.5)), int(round(y - 0.5))
        if not (0 <= i < nrows and 0 <= j < ncols):
            continue
        v = values[i, j]
        if not np.isfinite(v):
            continue
        lum = _rel_luminance(mesh.to_rgba(v))
        dark_on_light = lum > 0.42
        fg = "#000000" if dark_on_light else "#ffffff"
        halo = "#ffffff" if dark_on_light else "#000000"
        t.set_color(fg)
        t.set_path_effects([pe.withStroke(linewidth=1.1, foreground=halo, alpha=0.6)])


def render_grid(mat, spot, title, cbar_label, path, decimals=1):
    """Render one greek as a Skylit-style strike x expiry heatmap and save to ``path``.

    Dark background, viridis (min-max) colour scale, a white dashed spot line + tag,
    dates across the top, and the King strike starred.
    """
    strikes = list(mat.index)
    nrows, ncols = mat.shape
    # Landscape layout: wide columns so each cell is broad and the values are easy to read,
    # rows tall enough for a large font. With ~51 strike rows x 5 expiry columns this yields
    # a figure that is wider than it is tall (e.g. ~21" x ~19").
    height = max(11.0, 0.34 * nrows + 2.2)
    width = max(16.0, 4.0 + 3.4 * ncols)
    fig, ax = plt.subplots(figsize=(width, height))
    annot = _annot_grid(mat, decimals)
    mask = ~np.isfinite(mat.to_numpy(dtype=float))  # strikes absent for an expiry -> dark gap
    sns.heatmap(
        mat, ax=ax, cmap=SKY_CMAP, annot=annot, fmt="", mask=mask,
        annot_kws={"size": 11},  # color omitted -> seaborn picks per-cell contrast (dark on light, light on dark)
        linewidths=0.4, linecolor=SKY_GRID,
        cbar_kws={"label": cbar_label, "shrink": 0.6, "pad": 0.02},
    )
    cbar = ax.collections[0].colorbar if ax.collections else None
    _style_dark(fig, ax, cbar)
    if cbar is not None:
        cbar.ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: _fmt_k(x, 0)))
    _contrast_annotations(ax, mat)

    ax.xaxis.tick_top()
    ax.xaxis.set_label_position("top")
    ax.set_xticklabels(mat.columns, rotation=0, color=SKY_TEXT, fontsize=13)
    ax.set_yticklabels(strikes, rotation=0, color=SKY_TEXT, fontsize=10)
    ax.set_ylabel("")

    # Spot line + tag: rows run high->low, so the boundary sits below every strike > spot.
    n_above = sum(1 for k in strikes if k > spot)
    ax.axhline(n_above, color=SKY_SPOT, lw=2.2, ls=(0, (5, 2)), zorder=5)
    ax.annotate(
        f"spot ${spot:.2f}", xy=(0, n_above), xycoords=("axes fraction", "data"),
        xytext=(-10, 0), textcoords="offset points", ha="right", va="center",
        color=SKY_BG, fontsize=11.5, fontweight="bold", clip_on=False, zorder=6,
        bbox=dict(boxstyle="round,pad=0.35", fc=SKY_SPOT, ec="none"),
    )

    fig.suptitle(title, color=SKY_TEXT, fontsize=17, fontweight="bold")
    plt.savefig(path, dpi=200, facecolor=SKY_BG, bbox_inches="tight", pad_inches=0.35)
    plt.close(fig)


# Greek panels for the front-expiry triptych: (per_exp key, label, colorbar unit, decimals).
TRIPTYCH_PANELS = [
    ("gex", "Gamma (GEX)", "$K per 1% spot", 1),
    ("vex", "Vanna (VEX)", "$K per 1.00\u03c3", 1),
    ("cex", "Charm (CEX)", "$K per day", 1),
]


def render_front_triptych(per_exp, window, front_label, spot, title, path):
    """One expiry, three greeks (GEX/VEX/CEX) rendered side by side for a quick cross-read.

    Each panel is the front-expiry column of one greek as its OWN min-max viridis heatmap
    (independent colour scale + colorbar, because the three greeks live on very different
    dollar scales — a shared scale would wash the smaller one out). All three share the
    strike-row axis, a single white spot line crosses every panel, and each greek stars its
    own King ★ within its column.
    """
    strikes = sorted(window, reverse=True)
    nrows = len(strikes)
    height = max(11.0, 0.34 * nrows + 2.2)
    width = 16.5
    fig, axes = plt.subplots(1, len(TRIPTYCH_PANELS), figsize=(width, height))
    n_above = sum(1 for k in strikes if k > spot)  # spot line sits below every strike > spot

    for ax, (key, name, unit, dec) in zip(axes, TRIPTYCH_PANELS):
        mat = build_greek_matrix(per_exp[key], window, [front_label])  # strikes x 1 column
        mask = ~np.isfinite(mat.to_numpy(dtype=float))
        annot = _annot_grid(mat, dec)
        sns.heatmap(
            mat, ax=ax, cmap=SKY_CMAP, annot=annot, fmt="", mask=mask,
            annot_kws={"size": 12}, linewidths=0.4, linecolor=SKY_GRID, cbar=False,
        )
        cax = make_axes_locatable(ax).append_axes("right", size="16%", pad=0.08)
        cbar = fig.colorbar(ax.collections[0], cax=cax)
        cbar.ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: _fmt_k(x, 0)))
        _style_dark(fig, ax, cbar)
        _contrast_annotations(ax, mat)

        ax.set_title(f"{name}\n{unit}", color=SKY_TEXT, fontsize=14, fontweight="bold", pad=10)
        ax.set_xticks([])  # single column; the expiry is named in the suptitle
        ax.set_xlabel("")
        ax.set_ylabel("")
        ax.axhline(n_above, color=SKY_SPOT, lw=2.2, ls=(0, (5, 2)), zorder=5)
        if ax is axes[0]:
            ax.set_yticks(np.arange(nrows) + 0.5)
            ax.set_yticklabels(strikes, rotation=0, color=SKY_TEXT, fontsize=10)
            ax.annotate(
                f"spot ${spot:.2f}", xy=(0, n_above), xycoords=("axes fraction", "data"),
                xytext=(-10, 0), textcoords="offset points", ha="right", va="center",
                color=SKY_BG, fontsize=11.5, fontweight="bold", clip_on=False, zorder=6,
                bbox=dict(boxstyle="round,pad=0.35", fc=SKY_SPOT, ec="none"),
            )
        else:
            ax.set_yticks([])

    fig.suptitle(title, color=SKY_TEXT, fontsize=17, fontweight="bold")
    plt.savefig(path, dpi=200, facecolor=SKY_BG, bbox_inches="tight", pad_inches=0.35)
    plt.close(fig)


def _wall_color(s, side_color):
    """Tint a wall string by side, but leave an ``n/a`` (no wall) muted."""
    return side_color if isinstance(s, str) and s.startswith("$") else SKY_TEXT


def render_summary_table(rows, path, ticker, et, spot, slot=None):
    """Render the per-expiry magnet table as a dark Skylit-style PNG card.

    A clean tabular image (title + header + one row per expiry) so the figures stay
    aligned instead of wrapping the way a Discord monospace code block does on narrow
    screens. A heading carries the ticker, date and generation time; regime and the Σ
    columns are tinted green/red by sign; the call/put walls take the green/red side
    colours; the column legend sits at the foot. Numbers use a monospace face so digits
    line up. ``slot`` is accepted for parity with the scheduled alert; on-demand passes
    ``None`` and the heading simply shows the generation time.
    """
    # (header, x-anchor in axes fraction, horizontal alignment). The three Σ columns are
    # spread wide so a 7-8 digit value never collides with its neighbour.
    cols = [
        ("Exp",       0.015, "left"),
        ("DTE",       0.150, "center"),
        ("Reg",       0.212, "center"),
        ("Flip",      0.322, "right"),
        ("Call Wall", 0.442, "right"),
        ("Put Wall",  0.560, "right"),
        ("\u03a3GEX",   0.720, "right"),
        ("\u03a3Vanna", 0.860, "right"),
        ("\u03a3Charm", 0.996, "right"),
    ]
    n = max(len(rows), 1)
    width = 20.0
    height = 2.9 + 0.62 * n
    fig, ax = plt.subplots(figsize=(width, height))
    fig.patch.set_facecolor(SKY_BG)
    ax.set_facecolor(SKY_BG)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # Heading: ticker - date - generation time (so the card is self-contained).
    when = f"{slot[0]:02d}:{slot[1]:02d} ET" if slot else f"{et:%H:%M} ET"
    ax.text(0.5, 0.945, f"{ticker} Dealerflow \u2014 Gamma \u00b7 Vanna \u00b7 Charm Magnet Map",
            color=SKY_TEXT, fontsize=25, fontweight="bold", ha="center", va="center")
    ax.text(0.5, 0.873,
            f"{et:%a %b %d %Y}      \u00b7      generated {when}      \u00b7      spot ${spot:.2f}",
            color=SKY_MUTED, fontsize=14.5, ha="center", va="center")
    ax.axhline(0.83, color=SKY_GRID, lw=1.2)

    top, bottom = 0.76, 0.16
    dy = (top - bottom) / n  # header sits at `top`; each data row steps down by dy
    head_fs, cell_fs = 16, 15

    for label, x, align in cols:
        ax.text(x, top, label, color=SKY_TEXT, fontsize=head_fs, fontweight="bold",
                ha=align, va="center", family="monospace")
    ax.axhline(top - dy * 0.5, color=SKY_GRID, lw=1.6)

    for i, r in enumerate(rows):
        y = top - (i + 1) * dy
        if i % 2 == 1:
            ax.axhspan(y - dy * 0.5, y + dy * 0.5, color=SKY_ROW_ALT, zorder=0)
        reg_pos = r["regime"] == "positive"
        cells = [
            (r["exp"], SKY_TEXT),
            (str(r["dte"]), SKY_TEXT),
            ("+\u03b3" if reg_pos else "-\u03b3", SKY_POS if reg_pos else SKY_NEG),
            (r["flip_s"], _wall_color(r["flip_s"], SKY_TEXT)),
            (r["cw_s"], _wall_color(r["cw_s"], SKY_POS)),
            (r["pw_s"], _wall_color(r["pw_s"], SKY_NEG)),
            (_fmt_k(r["net_gex"], 0), SKY_POS if r["net_gex"] >= 0 else SKY_NEG),
            (_fmt_k(r["net_vex"], 0), SKY_POS if r["net_vex"] >= 0 else SKY_NEG),
            (_fmt_k(r["net_cex"], 0), SKY_POS if r["net_cex"] >= 0 else SKY_NEG),
        ]
        for (text, color), (_, x, align) in zip(cells, cols):
            ax.text(x, y, text, color=color, fontsize=cell_fs, ha=align, va="center",
                    family="monospace")

    ax.text(0.5, 0.06, SUMMARY_LEGEND, color=SKY_MUTED, fontsize=13,
            ha="center", va="center", family="monospace", style="italic")

    plt.savefig(path, dpi=200, facecolor=SKY_BG, bbox_inches="tight", pad_inches=0.3)
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
        out = ["**Positive \u03b3** \u2014 dealers fade moves; expect pinning / mean-reversion "
               "between the walls."]
    else:
        out = ["**Negative \u03b3** \u2014 dealers chase; expect trending / breakouts, walls are weak."]
    if flip:
        side = "above" if spot >= flip else "below"
        out.append(f"Spot ${spot:.2f} is {side} the \u03b3-flip {_rel(flip, spot)}.")
    if cw:
        out.append(f"Call-wall magnet {_rel(cw, spot)}.")
    if pw:
        out.append(f"Put-wall magnet {_rel(pw, spot)}.")
    return " ".join(out)


def build_summary_text(ticker, spot, source, et, rows):
    """Markdown header + plain-English magnet read — no table.

    The per-expiry table is rendered as a PNG card (``render_summary_table``) so it can't
    wrap on narrow Discord clients; this is the accompanying caption.
    """
    msg = f"# \U0001f9f2 {ticker} Dealerflow \u2014 Gamma \u00b7 Vanna \u00b7 Charm Magnet Map\n"
    msg += f"**{et:%a %b %d %Y} \u00b7 {et:%H:%M} ET** \u00b7 spot **${spot:.2f}** \u00b7 source `{source}` " \
           f"(data as of {et:%H:%M} ET)\n"
    if rows:
        msg += magnet_read(spot, rows[0]["keys"], rows[0]["regime"])
    else:
        msg += f"_No usable {ticker} expirations with open interest right now._"
    return msg


def build_summary(ticker, spot, source, et, rows):
    """Full plain-text summary incl. the fixed-width table — used for the local report
    artifact (Discord gets the header text + the table PNG instead)."""
    msg = build_summary_text(ticker, spot, source, et, rows)
    if rows:
        # Greek glyphs pulled out as names: an f-string expression part can't contain a
        # backslash escape on Python < 3.12.
        sg, sv, sc = "\u03a3GEX", "\u03a3Vanna", "\u03a3Charm"
        gp, gn = "+\u03b3", "-\u03b3"
        header = (f"{'Exp':<12}{'DTE':>4}{'Reg':>5}{'Flip':>8}{'CallWall':>10}"
                  f"{'PutWall':>9}{sg:>15}{sv:>15}{sc:>15}")
        lines = [header, "-" * len(header)]
        for r in rows:
            reg = gp if r["regime"] == "positive" else gn
            lines.append(
                f"{r['exp']:<12}{r['dte']:>4}{reg:>5}"
                f"{r['flip_s']:>8}{r['cw_s']:>10}{r['pw_s']:>9}"
                f"{_fmt_k(r['net_gex'], 0):>15}{_fmt_k(r['net_vex'], 0):>15}"
                f"{_fmt_k(r['net_cex'], 0):>15}")
        msg += "\n```\n" + "\n".join(lines) + "\n```"
        msg += "_" + SUMMARY_LEGEND + "._"
    return msg
