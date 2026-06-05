from datetime import datetime
import numpy as np


def _num(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # filter NaN


def black_scholes_gamma(S, K, T, r, sigma, option_type="call"):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    gamma = np.exp(-d1**2 / 2) / (S * sigma * np.sqrt(2 * np.pi * T))
    return gamma  # gamma is identical for calls and puts


def _expiry_T(expiry):
    if expiry:
        try:
            days = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days
            return max(0.01, days / 365.25)
        except (ValueError, TypeError):
            pass
    return 0.01


def _option_sign(row):
    ot = row.get("opt_type")
    if ot == "put":
        return -1
    if ot == "call":
        return 1
    # Fallback: OCC symbols put the C/P flag 9 chars from the end (before the strike).
    sym = str(row.get("contractSymbol", ""))
    return -1 if len(sym) >= 9 and sym[-9] == "P" else 1


def compute_gex_grid(df, spot, expiry=None):
    grid = {}
    T = _expiry_T(expiry)
    for _, row in df.iterrows():
        strike = _num(row.get("strike"))
        oi = _num(row.get("openInterest", 0))
        if oi <= 0 or strike <= 0:
            continue
        iv = _num(row.get("impliedVolatility", 0.3), 0.3)
        if iv <= 0:
            iv = 0.3
        gamma = black_scholes_gamma(spot, strike, T, 0.05, iv)
        gex = gamma * oi * 100 * spot * spot * _option_sign(row)
        grid[strike] = grid.get(strike, 0.0) + gex
    return grid


def get_key_levels(gex_grid):
    if not gex_grid:
        return {"gamma_flip": 0.0, "call_wall": 0.0, "put_wall": 0.0}
    strikes = sorted(gex_grid)
    # Gamma flip ≈ the strike where cumulative GEX (summed low→high strike) crosses
    # zero. This sits near the balance of put (negative) and call (positive) gamma,
    # unlike "smallest |GEX|" which would always pick a deep-OTM ~zero-gamma strike.
    cum = 0.0
    flip = strikes[0]
    crossed = False
    for i, k in enumerate(strikes):
        prev = cum
        cum += gex_grid[k]
        if i > 0 and ((prev < 0 <= cum) or (prev > 0 >= cum)):
            flip = k if abs(cum) <= abs(prev) else strikes[i - 1]
            crossed = True
            break
    if not crossed:
        flip = min(strikes, key=lambda k: abs(gex_grid[k]))
    positives = {k: v for k, v in gex_grid.items() if v > 0}
    negatives = {k: v for k, v in gex_grid.items() if v < 0}
    call_wall = max(positives, key=positives.get) if positives else 0.0
    put_wall = min(negatives, key=negatives.get) if negatives else 0.0
    return {"gamma_flip": float(flip), "call_wall": float(call_wall), "put_wall": float(put_wall)}


def get_regime(gex_grid):
    total_gex = sum(gex_grid.values())
    return "positive" if total_gex > 0 else "negative"
