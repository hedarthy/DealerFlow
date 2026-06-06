"""Dealer-positioning greeks and exposure profiles (vendored for the SPY alert).

Self-contained copy of the Black-Scholes gamma/vanna/charm math used to build the
per-strike dealer-signed exposure grids (calls +1, puts -1). No scipy dependency —
the normal CDF/PDF come from ``math``. Kept independent of ``src/`` so this alert
can never be broken by, or break, the twice-daily screener.

- GEX (gamma exposure): dealer-delta $ shift per 1% spot move.
- VEX (vanna exposure): dealer-delta $ shift per 1.00 sigma (a full vol point) change in IV.
- CEX (charm exposure): dealer-delta $ shift per calendar day (decay).
"""
from datetime import datetime
from math import erf, exp, log, pi, sqrt

from spy_gex.calendar_util import eastern_now

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

    Gamma and vanna are identical for calls and puts; charm is too when the dividend
    yield is zero, which we assume. Vanna is per 1.00 (i.e. 100 vol points) change in
    sigma; charm is per year. Dealer sign is applied later.
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

    Using calendar ``.days`` collapses every 0-2 DTE option to a ~0.01yr floor,
    badly distorting the greeks for exactly the chains this targets. We instead use
    seconds-to-close and floor at one hour so near-expiry gamma stays large-but-finite
    rather than blowing up at T->0.
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

    - GEX: $ of dealer delta per 1% spot move    (gamma * notional * S^2 * 0.01)
    - VEX: $ of dealer delta per 1.00 sigma move  (vanna is already per 1.00 sigma)
    - CEX: $ of dealer delta per calendar day      (charm is per year -> / 365.25)

    VEX is quoted per a full 1.00 change in implied vol (i.e. one whole vol point,
    e.g. 0.20 -> 1.20), matching the per-1.00-sigma convention common to vendor
    dealer-flow maps; that is 100x a per-0.01-vol-point figure.
    """
    gamma, vanna, charm = bs_greeks(spot, strike, T, RISK_FREE, iv)
    notional = oi * 100 * sign
    gex = gamma * notional * spot * spot * 0.01   # per 1% spot move
    vex = vanna * notional * spot                 # per 1.00 sigma (vanna already per 1.00)
    cex = charm * notional * spot / 365.25        # per calendar day (charm per year)
    return gex, vex, cex


def compute_exposure_grids(df, spot, expiry=None):
    """Aggregate GEX/VEX/CEX per strike across an option chain.

    Contracts with no open interest, a non-positive strike, or no usable implied
    volatility are skipped: fabricating an IV invents dealer exposure that isn't
    really there and pollutes the regime/flip signals.
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


def cumulative_zero_cross(grid, spot=None, window=0.25, min_frac=0.05):
    """Strike where cumulative exposure (summed low->high strike) crosses zero — the
    gamma/vanna "flip", the balance point between negative (put-side) and positive
    (call-side) exposure, which should sit near spot.

    For 0-2 DTE chains, exposure at deep-OTM strikes is ~0 but carries tiny mixed
    signs that can trigger a spurious crossing in the wings. We guard against that:

    * ``window`` (±25% of spot) restricts the search to near-money strikes. If spot is
      given but no strike falls in that window we return ``0.0``.
    * ``min_frac`` ignores any crossing where the cumulative never moved past that
      fraction (5%) of the *windowed* gross exposure (wing wiggles around zero).
    * When several genuine crossings remain we return the one nearest spot.

    A genuinely one-sided near-money book has no flip in range; we return ``0.0`` so
    callers treat the structure as neutral rather than inventing a far, unstable level.
    """
    if not grid:
        return 0.0
    all_strikes = sorted(grid)
    strikes = all_strikes
    base = 0.0
    if spot and spot > 0:
        lo, hi = spot * (1.0 - window), spot * (1.0 + window)
        windowed = [k for k in all_strikes if lo <= k <= hi]
        if not windowed:
            return 0.0
        strikes = windowed
        base = sum(grid[k] for k in all_strikes if k < lo)  # carry below-window mass

    gross = sum(abs(grid[k]) for k in strikes)
    thresh = min_frac * gross if gross else 0.0

    crossings = []
    cum = base
    prev_c = base
    prev_s = None
    for k in strikes:
        cum += grid[k]
        if (prev_c < 0 <= cum) or (prev_c > 0 >= cum):
            if max(abs(prev_c), abs(cum)) >= thresh:
                if prev_s is None or not (spot and spot > 0):
                    crossings.append(k)
                else:
                    chosen = min(((prev_s, prev_c), (k, cum)),
                                 key=lambda sc: (abs(sc[0] - spot), abs(sc[1])))[0]
                    crossings.append(chosen)
        prev_s, prev_c = k, cum

    if crossings:
        if spot and spot > 0:
            return float(min(crossings, key=lambda s: abs(s - spot)))
        return float(crossings[0])
    return 0.0


def get_key_levels(gex_grid, spot=None):
    if not gex_grid:
        return {"gamma_flip": 0.0, "call_wall": 0.0, "put_wall": 0.0}
    flip = cumulative_zero_cross(gex_grid, spot)
    positives = {k: v for k, v in gex_grid.items() if v > 0}
    negatives = {k: v for k, v in gex_grid.items() if v < 0}
    call_wall = max(positives, key=positives.get) if positives else 0.0
    put_wall = min(negatives, key=negatives.get) if negatives else 0.0
    return {"gamma_flip": float(flip), "call_wall": float(call_wall), "put_wall": float(put_wall)}


def select_window_strikes(strikes, spot, n=25):
    """Centered strike window: up to ``n`` strikes at/below ``spot`` plus up to ``n``
    strikes above it, returned as a single ascending list.

    A strike exactly equal to spot is grouped with the at/below side. Having fewer
    than ``n`` strikes on a side (deep ITM/OTM sparsity) is fine — we return whatever
    exists. Used by the SPY heatmaps to show "25 up / 25 down".
    """
    uniq = sorted({float(k) for k in strikes if k is not None})
    if not uniq:
        return []
    if not (spot and spot > 0):
        return uniq[: 2 * n]
    below = [k for k in uniq if k <= spot][-n:]
    above = [k for k in uniq if k > spot][:n]
    return below + above


def get_regime(gex_grid):
    total_gex = sum(gex_grid.values())
    return "positive" if total_gex > 0 else "negative"


def get_vanna_regime(vex_grid):
    """Sign of net dealer vanna. With VEX = d(dealer delta)/d(sigma), a positive net
    forces dealer *buying* when IV falls (price support) and selling when IV rises; a
    negative net does the opposite."""
    total = sum(vex_grid.values())
    return "positive" if total > 0 else "negative"
