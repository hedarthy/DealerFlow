"""Dealer-positioning greeks and exposure profiles.

Computes per-contract Black-Scholes gamma, vanna and charm (no scipy dependency —
the normal CDF/PDF come from ``math``), then aggregates them into per-strike
exposure grids signed by dealer positioning (calls +1, puts -1).

- GEX  (gamma exposure): dealer-delta $ shift per 1% spot move.
- VEX  (vanna exposure): dealer-delta $ shift per 1.00 sigma (full vol point) IV move.
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

    - GEX: $ of dealer delta per 1% spot move    (gamma * notional * S^2 * 0.01)
    - VEX: $ of dealer delta per 1.00 sigma move  (vanna is already per 1.00 sigma)
    - CEX: $ of dealer delta per calendar day      (charm is per year -> / 365.25)

    VEX is quoted per a full 1.00 change in implied vol (one whole sigma, e.g.
    0.20 -> 1.20), matching the per-1.00-sigma convention common to vendor dealer-flow
    maps and to the standalone SPY alert (spy_gex/). That is 100x a per-0.01-vol-point
    figure, but it is a uniform positive rescale: per-grid rankings, flip locations and
    the normalised score components are all unchanged — only the displayed magnitude
    differs.
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


def cumulative_zero_cross(grid, spot=None, window=0.25, min_frac=0.05):
    """Strike where cumulative exposure (summed low->high strike) crosses zero.

    This is the gamma/vanna "flip" — the balance point between negative (put-side)
    and positive (call-side) exposure — and should sit near spot, unlike "smallest
    |x|" which would always pick a deep-OTM ~zero strike.

    For 0-2 DTE chains, exposure at deep-OTM strikes is ~0 but carries tiny mixed
    signs that can trigger a spurious crossing in the wings. We guard against that in
    two ways so the flip stays a meaningful, stable near-money level:

    * ``window`` (±25% of spot) restricts the search to near-money strikes. If spot is
      given but no strike falls in that window we return ``0.0`` rather than scanning the
      full chain (which would reintroduce far-flip instability on a stale/split spot).
    * ``min_frac`` ignores any crossing where the cumulative never moved past that
      fraction (5%) of the *windowed* gross exposure — i.e. wing wiggles around zero.
      Scaling to windowed (not whole-chain) gross keeps one huge far-OTM strike from
      inflating the bar and masking a real near-money flip.
    * When several genuine crossings remain we return the one **nearest spot**, not
      the first one encountered scanning upward (which could be a low-strike blip).

    A genuinely one-sided near-money book has no flip in range; we return ``0.0`` so
    callers treat the structure as neutral (the scorer's directional overlay and the
    strategy bullets both special-case a zero/unknown flip) rather than inventing a
    far, unstable level.
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
            # Spot is set but no strike sits within the search window (stale/split spot
            # or a malformed chain). Refuse to invent a far flip; treat as neutral.
            return 0.0
        strikes = windowed
        base = sum(grid[k] for k in all_strikes if k < lo)  # carry below-window mass

    # Scale the significance threshold to the exposure actually in play across the
    # scanned (near-money) strikes — NOT the whole chain — so a single huge far-OTM
    # strike outside the window can't inflate the bar and mask a real near-money flip.
    gross = sum(abs(grid[k]) for k in strikes)
    thresh = min_frac * gross if gross else 0.0

    crossings = []
    cum = base
    prev_c = base
    prev_s = None
    for k in strikes:
        cum += grid[k]
        if (prev_c < 0 <= cum) or (prev_c > 0 >= cum):
            # Only count it if the cumulative actually carried real mass on one side
            # (kills deep-OTM wing wiggles around zero).
            if max(abs(prev_c), abs(cum)) >= thresh:
                if prev_s is None or not (spot and spot > 0):
                    crossings.append(k)
                else:
                    # Attribute the flip to a bracketing strike: nearest spot, and on a
                    # distance tie the endpoint whose cumulative is closer to zero (i.e.
                    # nearer the true crossing). Keeps a big jump out of a far wing from
                    # pinning the flip on the wrong side.
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


def select_window_strikes(strikes, spot, n=25, must_include=None):
    """Centered strike window: up to ``n`` strikes at/below ``spot`` plus up to ``n``
    above it, returned as a single ascending list.

    A strike exactly equal to spot is grouped with the at/below side. Having fewer than
    ``n`` strikes on a side (deep ITM/OTM sparsity) is fine. ``must_include`` (e.g. the
    traded strike) is force-added even if it falls outside the centered window, so a
    near-but-not-nearest pick strike is always visible on its own heatmap. Used to frame
    the dealer structure around spot the way the SPY heatmaps do (25 up / 25 down).
    """
    uniq = sorted({float(k) for k in strikes if k is not None})
    if not uniq:
        return [float(must_include)] if must_include else []
    if not (spot and spot > 0):
        win = uniq[: 2 * n]
    else:
        below = [k for k in uniq if k <= spot][-n:]
        above = [k for k in uniq if k > spot][:n]
        win = below + above
    if must_include is not None:
        mi = float(must_include)
        if mi not in win:
            win = sorted(set(win) | {mi})
    return win


def get_regime(gex_grid):
    total_gex = sum(gex_grid.values())
    return "positive" if total_gex > 0 else "negative"


def get_vanna_regime(vex_grid):
    """Sign of net dealer vanna. With VEX = d(dealer delta)/d(sigma), a positive net
    forces dealer *buying* when IV falls (price support) and selling when IV rises;
    a negative net does the opposite."""
    total = sum(vex_grid.values())
    return "positive" if total > 0 else "negative"
