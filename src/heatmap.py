"""Skylit-style dealer-flow heatmaps for the high-conviction screener.

This is a self-contained copy of the visual layer proven out in the standalone SPY
alert (``spy_gex/agent.py``): a dark, viridis, value-annotated strike heatmap with a
white spot line and a starred "King" strike. It is *vendored* (copied, not imported)
on purpose so ``src/`` and ``spy_gex/`` stay independent — neither pipeline can break
the other.

The screener's twist on the SPY triptych: instead of one greek across five expiries,
each high-conviction pick gets ONE expiry across three greeks (GEX / VEX / CEX) side
by side, framed on a spot-centred strike window, with the *traded* strike outlined in
gold so the reader can instantly see where the pick sits inside the dealer structure.
"""
import matplotlib
matplotlib.use("Agg")  # no-op if the caller already selected Agg; keeps direct imports headless
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter
from mpl_toolkits.axes_grid1 import make_axes_locatable
import seaborn as sns

# Skylit-AI-style dark heatmap palette (viridis on near-black, white text/spot tag).
SKY_BG = "#0b0d10"
SKY_TEXT = "#e6e8eb"
SKY_GRID = "#1b1f24"
SKY_SPOT = "#ffffff"
SKY_CMAP = "viridis"
SKY_KING = "★"
SKY_PICK = "#f2c744"   # gold outline + tag marking the traded strike

# One expiry, three greeks: (grid key, panel title, colorbar unit, decimals).
TRIPTYCH_PANELS = [
    ("gex", "Gamma (GEX)", "$K per 1% spot", 1),
    ("vex", "Vanna (VEX)", "$K per 1.00σ", 1),
    ("cex", "Charm (CEX)", "$K per day", 1),
]


def _fmt_k(v, decimals=1):
    """Format a $-thousands value Skylit-style: ``$1,234.5K`` / ``-$1,234.5K``."""
    sign = "-$" if v < 0 else "$"
    return f"{sign}{abs(v):,.{decimals}f}K"


def build_greek_matrix(per_exp, window, exp_labels, divisor=1e3):
    """Strike-rows (descending) × expiry-columns matrix in $K. Absent strikes -> NaN."""
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
    """Cell strings: blank missing/near-zero cells, star the single King (max |value|)."""
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
    """Recolour each cell's number to black/white for max contrast against its own cell."""
    if not ax.collections:
        return
    mesh = ax.collections[0]
    values = mat.to_numpy(dtype=float)
    nrows, ncols = values.shape
    for t in ax.texts:
        x, y = t.get_position()
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


def render_pick_triptych(contract, gex_grid, vex_grid, cex_grid, path):
    """Render one pick's GEX/VEX/CEX over a spot-centred strike window to ``path``.

    Three independent min-max viridis panels (the greeks live on very different dollar
    scales) sharing one strike-row axis. A white dashed spot line crosses all three; the
    traded strike is outlined in gold and tagged so the pick is unmistakable. The window
    is centred on spot but always includes the traded strike even if it sits outside it.
    """
    from src.gex_calculator import select_window_strikes

    spot = float(contract.get("spot") or 0.0)
    pick_strike = float(contract["strike"])
    exp_label = contract["exp"]
    side = "C" if contract.get("type") == "call" else "P"

    universe = set(gex_grid) | set(vex_grid) | set(cex_grid) | {pick_strike}
    window = select_window_strikes(universe, spot, n=25, must_include=pick_strike)
    if not window:
        raise ValueError("no strikes to render")
    strikes = sorted(window, reverse=True)
    nrows = len(strikes)
    pick_row = strikes.index(pick_strike) if pick_strike in strikes else None
    n_above = sum(1 for k in strikes if k > spot)  # spot line sits below every strike > spot

    per_exp_by_key = {
        "gex": {exp_label: gex_grid},
        "vex": {exp_label: vex_grid},
        "cex": {exp_label: cex_grid},
    }

    height = max(11.0, 0.34 * nrows + 2.2)
    fig, axes = plt.subplots(1, len(TRIPTYCH_PANELS), figsize=(16.5, height))

    for ax, (key, name, unit, dec) in zip(axes, TRIPTYCH_PANELS):
        mat = build_greek_matrix(per_exp_by_key[key], window, [exp_label])
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
        ax.set_xticks([])
        ax.set_xlabel("")
        ax.set_ylabel("")
        ax.axhline(n_above, color=SKY_SPOT, lw=2.2, ls=(0, (5, 2)), zorder=5)

        # Gold outline on the traded strike's row across every panel.
        if pick_row is not None:
            ax.add_patch(Rectangle((0, pick_row), 1, 1, fill=False,
                                   edgecolor=SKY_PICK, lw=2.6, zorder=7))

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

        # Pick tag on the rightmost panel, pointing at the gold row.
        if pick_row is not None and ax is axes[-1]:
            ax.annotate(
                f"◀ pick {pick_strike:g}{side}", xy=(1, pick_row + 0.5),
                xycoords=("axes fraction", "data"), xytext=(12, 0),
                textcoords="offset points", ha="left", va="center",
                color=SKY_BG, fontsize=11.5, fontweight="bold", clip_on=False, zorder=8,
                bbox=dict(boxstyle="round,pad=0.35", fc=SKY_PICK, ec="none"),
            )

    title = (f"{contract['ticker']}  {pick_strike:g}{side}  ·  exp {exp_label}  "
             f"·  dealer GEX / VEX / CEX")
    fig.suptitle(title, color=SKY_TEXT, fontsize=17, fontweight="bold")
    plt.savefig(path, dpi=200, facecolor=SKY_BG, bbox_inches="tight", pad_inches=0.35)
    plt.close(fig)
