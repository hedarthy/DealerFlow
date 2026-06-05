"""Dealer-positioning greeks and exposure profiles.

Computes per-contract Black-Scholes gamma, vanna and charm (no scipy dependency —
the normal CDF/PDF come from ``math``), then aggregates them into per-strike
exposure grids signed by dealer positioning (calls +1, puts -1).

- GEX  (gamma exposure): dealer-delta $ shift per 1% spot move.
- VEX  (vanna exposure): dealer-delta $ shift per 1 vol-point change in IV.
- CEX  (charm exposure): dealer-delta $ shift per calendar day (decay).
"""

from datetime import datetime
from math import erf, exp, log, pi, sqrt

from src.timeutil import eastern_now

RISK_FREE = 0.05
_SQRT2 = sqrt(2.0)
_SQRT2PI = sqrt(2.0 * pi)


def _num(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # filter NaN


def _norm_pdf(x):
    return exp(-0.5 * x * x) / _SQRT2PI


def _norm_cdf(x):
    return 0.5 * (1.0 + erf(x / _SQRT2))


def bs_greeks(S, K, T, r, sigma):
    """Return per-share gamma, vanna and charm for a European option.

    Gamma and vanna are identical for calls and puts; charm is too when the
    dividend yield is zero, which we assume. Vanna is per 1.00 (i.e. 100 vol
    points) change in sigma; charm is per year. Dealer sign is applied later.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0, 0.0, 0.0
    sqrtT = sqrt(T)
    d1 = (log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    pdf = _norm_pdf(d1)
    gamma = pdf / (S * sigma * sqrtT)
    vanna = -pdf * d2 / sigma
    charm = -pdf * (2.0 * r * T - d2 * sigma * sqrtT) / (2.0 * T * sigma * sqrtT)
    return gamma, vanna, charm


def _expiry_T(expiry):
    """Year-fraction to expiry, measured to the 16:00 close on the expiry date.

    Using calendar ``.days`` (the old approach) collapses every 0-2 DTE option to a
    ~0.01yr floor (~3.65 days), badly distorting the greeks for exactly the chains
    this screener targets. We instead use seconds-to-close and floor at one hour so
    near-expiry gamma stays large-but-finite rather than blowing up at T->0.
    """
    floor = 1.0 / (365.25 * 24.0)  # one hour, in years
    if expiry:
        try:
            exp = datetime.strptime(expiry, "%Y-%m-%d").replace(hour=16, minute=0)
            secs = (exp - eastern_now()).total_seconds()
            return max(floor, secs / (365.25 * 24.0 * 3600.0))
        except (ValueError, TypeError):
            pass
    return floor


def _option_sign(row):
    ot = row.get("opt_type")
    if ot == "put":
        return -1
    if ot == "call":
        return 1
    # Fallback: OCC symbols put the C/P flag 9 chars from the end (before the strike).
    sym = str(row.get("contractSymbol", ""))
    return -1 if len(sym) >= 9 and sym[-9] == "P" else 1


def contract_exposures(spot, strike, T, iv, oi, sign):
    """Dealer-signed gamma/vanna/charm exposure for a single contract, in the
    conventional desk units used across the dealer-positioning literature:

    - GEX: $ of dealer delta per 1% spot move   (gamma * notional * S^2 * 0.01)
    - VEX: $ of dealer delta per 1 vol-point     (vanna is per 1.00 sigma -> * 0.01)
    - CEX: $ of dealer delta per calendar day     (charm is per year -> / 365.25)

    These are uniform positive rescales of the raw greeks, so per-grid rankings,
    flip locations and normalised score components are unchanged — only the
    displayed magnitudes become interpretable.
    """
    gamma, vanna, charm = bs_greeks(spot, strike, T, RISK_FREE, iv)
    notional = oi * 100 * sign
    gex = gamma * notional * spot * spot * 0.01   # per 1% spot move
    vex = vanna * notional * spot * 0.01          # per 1 vol-point (sigma per 1.00)
    cex = charm * notional * spot / 365.25        # per calendar day (charm per year)
    return gex, vex, cex


def compute_exposure_grids(df, spot, expiry=None):
    """Aggregate GEX/VEX/CEX per strike across an option chain.

    Contracts with no open interest, a non-positive strike, or no usable implied
    volatility are skipped: fabricating an IV (the old code forced 0.3) invents
    dealer exposure that isn't really there and pollutes the regime/flip signals.
    """
    gex, vex, cex = {}, {}, {}
    T = _expiry_T(expiry)
    for _, row in df.iterrows():
        strike = _num(row.get("strike"))
        oi = _num(row.get("openInterest", 0))
        iv = _num(row.get("impliedVolatility", 0.0))
        if oi <= 0 or strike <= 0 or iv <= 0:
            continue
        ge, ve, ce = contract_exposures(spot, strike, T, iv, oi, _option_sign(row))
        gex[strike] = gex.get(strike, 0.0) + ge
        vex[strike] = vex.get(strike, 0.0) + ve
        cex[strike] = cex.get(strike, 0.0) + ce
    return gex, vex, cex


def compute_gex_grid(df, spot, expiry=None):
    """Backward-compatible helper returning only the gamma-exposure grid."""
    return compute_exposure_grids(df, spot, expiry)[0]


def cumulative_zero_cross(grid, spot=None, window=0.25):
    """Strike where cumulative exposure (summed low->high strike) crosses zero.

    This is the gamma/vanna "flip" — the balance point between negative (put-side)
    and positive (call-side) exposure — and sits near spot, unlike "smallest |x|"
    which would always pick a deep-OTM ~zero strike.

    For 0-2 DTE chains, exposure at deep-OTM strikes is ~0 but carries tiny mixed
    signs that can trigger a spurious early crossing in the wings. When ``spot`` is
    supplied we therefore restrict the search to strikes within ``window`` (±25% by
    default) of spot, so the flip reflects the meaningful near-money transition.

    When the (windowed) cumulative profile never changes sign — a one-sided book —
    the flip lies outside the listed strikes; we then return the peak-|exposure|
    strike, which for short-dated chains concentrates near spot.
    """
    if not grid:
        return 0.0
    all_strikes = sorted(grid)
    strikes = all_strikes
    cum = 0.0
    if spot and spot > 0:
        lo, hi = spot * (1.0 - window), spot * (1.0 + window)
        windowed = [k for k in all_strikes if lo <= k <= hi]
        if windowed:
            strikes = windowed
            cum = sum(grid[k] for k in all_strikes if k < lo)  # carry below-window mass
    prev = cum
    for idx, k in enumerate(strikes):
        cum += grid[k]
        if (prev < 0 <= cum) or (prev > 0 >= cum):
            if idx == 0:
                return float(k)
            return float(k if abs(cum) <= abs(prev) else strikes[idx - 1])
        prev = cum
    return float(max(strikes, key=lambda s: abs(grid[s])))


def get_key_levels(gex_grid, spot=None):
    if not gex_grid:
        return {"gamma_flip": 0.0, "call_wall": 0.0, "put_wall": 0.0}
    flip = cumulative_zero_cross(gex_grid, spot)
    positives = {k: v for k, v in gex_grid.items() if v > 0}
    negatives = {k: v for k, v in gex_grid.items() if v < 0}
    call_wall = max(positives, key=positives.get) if positives else 0.0
    put_wall = min(negatives, key=negatives.get) if negatives else 0.0
    return {"gamma_flip": float(flip), "call_wall": float(call_wall), "put_wall": float(put_wall)}


def get_regime(gex_grid):
    total_gex = sum(gex_grid.values())
    return "positive" if total_gex > 0 else "negative"


def get_vanna_regime(vex_grid):
    """Sign of net dealer vanna. With VEX = d(dealer delta)/d(sigma), a positive net
    forces dealer *buying* when IV falls (price support) and selling when IV rises;
    a negative net does the opposite."""
    total = sum(vex_grid.values())
    return "positive" if total > 0 else "negative"
