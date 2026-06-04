from datetime import datetime
import numpy as np


def black_scholes_gamma(S, K, T, r, sigma, option_type="call"):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    gamma = np.exp(-d1**2 / 2) / (S * sigma * np.sqrt(2 * np.pi * T))
    return gamma  # gamma is identical for calls and puts


def compute_gex_grid(df, spot):
    grid = {}
    for _, row in df.iterrows():
        strike = row["strike"]
        oi = row.get("openInterest", 0)
        if oi == 0:
            continue
        try:
            exp_raw = row.name.split()[0] if isinstance(row.name, str) else "2026-06-01"
            T = max(0.01, (datetime.strptime(exp_raw, "%Y-%m-%d") - datetime.now()).days / 365.25)
        except (ValueError, AttributeError):
            T = 0.01
        gamma = black_scholes_gamma(spot, strike, T, 0.05, row.get("impliedVolatility", 0.3))
        sign = -1 if str(row.get("contractSymbol", "")).find("P") > 0 else 1
        gex = gamma * oi * 100 * spot * spot * sign
        grid[strike] = grid.get(strike, 0) + gex
    return grid


def get_key_levels(gex_grid):
    if not gex_grid:
        return {"gamma_flip": 0, "call_wall": 0, "put_wall": 0}
    sorted_strikes = sorted(gex_grid.keys())
    flip = min(sorted_strikes, key=lambda k: abs(gex_grid[k]))
    call_wall = max(gex_grid, key=lambda k: gex_grid[k] if gex_grid[k] > 0 else -np.inf)
    put_wall = min(gex_grid, key=lambda k: gex_grid[k] if gex_grid[k] < 0 else np.inf)
    return {"gamma_flip": flip, "call_wall": call_wall, "put_wall": put_wall}


def get_regime(gex_grid):
    total_gex = sum(gex_grid.values())
    return "positive" if total_gex > 0 else "negative"
