import math


def _num(x, default=0.0):
    """Coerce to float, mapping None/non-numeric/NaN to a default."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # v != v is True only for NaN


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def score_components(row, spot, dte=1, gex_balance=0.0, em_pct=0.0,
                     vanna_ex=0.0, charm_ex=0.0, max_vex=0.0, max_cex=0.0):
    """Return each 0-100 score component (unweighted).

    Every component is *continuous* so liquid names spread across the range instead
    of piling onto a few near-binary plateaus (the old design used 100/40 and 100/25
    step functions plus a flow term that saturated at vol/OI>=5, which made almost
    every liquid contract score the same ~75).
    """
    oi = _num(row.get("openInterest", 0))
    vol = _num(row.get("volume", 0))
    strike = _num(row.get("strike", spot), spot)

    # Order flow: smooth saturation, no hard cap pileup (~63 at vol/OI=5, ~95 at 15).
    vol_oi = vol / oi if oi > 0 else vol
    flow_proxy = 100.0 * (1.0 - math.exp(-vol_oi / 5.0))

    # Liquidity: continuous in log-OI (0 at OI=100, 50 at 1k, 100 at 10k+).
    squeeze = _clamp((math.log10(max(oi, 1.0)) - 2.0) / 2.0 * 100.0)

    # Regime: scale-free net dealer gamma balance in [-1,1] -> ~[7,93], 50 at neutral.
    gex_score = 50.0 + 50.0 * math.tanh(2.0 * gex_balance)

    # Dealer vanna/charm concentration at this strike vs the chain's largest net strike.
    v = abs(vanna_ex) / max_vex if max_vex else 0.0
    c = abs(charm_ex) / max_cex if max_cex else 0.0
    vanna_charm = _clamp(100.0 * (0.6 * v + 0.4 * c))

    # Moneyness scaled by the expected move (IV*sqrt(T)) so it is comparable across a
    # low-vol index and a high-vol small cap; falls to 0 about 1.5 expected moves out.
    dist_pct = abs(strike - spot) / spot * 100.0 if spot else 0.0
    if em_pct > 0:
        moneyness = _clamp(100.0 * (1.0 - dist_pct / (1.5 * em_pct)))
    else:
        moneyness = _clamp(100.0 - dist_pct * 8.0)  # fallback if no usable IV
    dte_factor = {0: 1.0, 1: 0.9, 2: 0.8}.get(int(dte), 0.7)
    moneyness_dte = moneyness * dte_factor

    return {
        "gex_regime": gex_score,
        "flow_proxy": flow_proxy,
        "squeeze": squeeze,
        "vanna_charm": vanna_charm,
        "moneyness_dte": moneyness_dte,
    }


def weighted_score(components, weights):
    """Weighted 0-100 composite from a component dict (weights sum to 1.0)."""
    return float(_clamp(sum(weights[k] * components[k] for k in components)))


_EDGE_LABEL = {
    "gex_regime": "regime",
    "flow_proxy": "flow",
    "squeeze": "liquidity",
    "vanna_charm": "vanna",
    "moneyness_dte": "ATM",
}


def dominant_edge(components, weights):
    """Short label for the highest weighted-contribution component — the 'why'."""
    contrib = {k: weights[k] * components[k] for k in components}
    return _EDGE_LABEL[max(contrib, key=contrib.get)]


def price_action_adjustment(opt_type, spot, ema8, ema21,
                            align_bonus=12.0, oppose_penalty=15.0, tol=0.001):
    """SeanTrades 8/21 EMA-stack confirmation, returned as additive score points.

    A bullish stack (spot > ema8 > ema21) confirms calls and opposes puts; a bearish
    stack (spot < ema8 < ema21) confirms puts and opposes calls; price tangled in the
    EMAs is "mixed" (no opinion). A small ``tol`` band keeps a spot sitting right on an
    EMA from flip-flopping the signal. This is layered as an additive overlay rather
    than a weighted component so it can be toggled off without disturbing the normalised
    component weights — a confirmed trade is nudged up, a counter-trend one docked and
    (in the agent) barred from the top-two high-conviction picks.

    Returns ``(points, label)``: points > 0 confirms, < 0 opposes, 0 is neutral.
    """
    s, e8, e21 = _num(spot), _num(ema8), _num(ema21)
    if s <= 0 or e8 <= 0 or e21 <= 0:
        return 0.0, "n/a"
    is_call = str(opt_type).lower().startswith("c")
    if s > e8 * (1 + tol) and e8 > e21 * (1 + tol):
        return (align_bonus, "8/21 bull stack ✓") if is_call \
            else (-oppose_penalty, "counter 8/21 bull stack ✗")
    if s < e8 * (1 - tol) and e8 < e21 * (1 - tol):
        return (align_bonus, "8/21 bear stack ✓") if not is_call \
            else (-oppose_penalty, "counter 8/21 bear stack ✗")
    return 0.0, "EMAs mixed"


def gex_directional_adjustment(opt_type, spot, gamma_flip, call_wall, put_wall, regime,
                               em_pct=0.0, align_bonus=10.0, oppose_penalty=12.0):
    """Dealer-positioning *directional* confirmation, returned as additive points.

    The five weighted components and the gamma/vanna/charm magnitudes are all
    side-symmetric, so the base score barely distinguishes a call from a put on the
    same strike — directional conviction otherwise rests entirely on the EMA overlay.
    This overlay supplies the missing read from the GEX *structure*, but only where
    that structure is actually reliable.

    We assert an edge **only in a positive-gamma (mean-reverting) book**, where the
    gamma flip is genuine support and the walls are genuine magnets: a call is
    confirmed when spot sits above the flip with real room up to the call wall, a put
    when spot sits below the flip with room down to the put wall. Chasing past a wall
    (move already made, dealers selling/buying into it) or clearly fighting the flip is
    penalised; sitting *at* a wall or inside a dead-zone around the flip is neutral so
    momentum/EMA decides (a flip reclaim is a long, not a short). In a negative-gamma
    (trending) book the walls are weak and price accelerates, so we abstain (0) and
    defer entirely to the price-action overlay — never fading a breakout. The flip
    dead-zone and wall-room bands scale with the expected move so volatile names need
    proportionally more room before a fresh entry is confirmed.

    Returns ``(points, label)``: > 0 confirms, < 0 opposes, 0 is neutral.
    """
    s, flip = _num(spot), _num(gamma_flip)
    cw, pw = _num(call_wall), _num(put_wall)
    if s <= 0 or flip <= 0:
        return 0.0, "n/a"
    if regime != "positive":
        return 0.0, "neg-γ: momentum-led (neutral)"
    em = max(0.0, _num(em_pct)) / 100.0
    flip_band = max(0.0025, 0.30 * em)   # ± dead-zone around the flip (reclaim zone)
    wall_room = max(0.0040, 0.40 * em)   # min room to a wall to still be a fresh entry
    tol = 0.001
    is_call = str(opt_type).lower().startswith("c")
    if is_call:
        if cw > 0 and s > cw * (1 + tol):
            return -oppose_penalty, "spot above call wall — upside capped ✗"
        if cw > 0 and s >= cw * (1 - wall_room):
            return 0.0, "at call wall — limited upside"
        if s > flip * (1 + flip_band) and (cw <= 0 or s < cw * (1 - wall_room)):
            return align_bonus, "above γ-flip, room to call wall ✓"
        if s < flip * (1 - flip_band):
            return -oppose_penalty, "below γ-flip support ✗"
        return 0.0, "near γ-flip — momentum decides"
    if pw > 0 and s < pw * (1 - tol):
        return -oppose_penalty, "spot below put wall — downside capped ✗"
    if pw > 0 and s <= pw * (1 + wall_room):
        return 0.0, "at put wall — limited downside"
    if pw > 0 and s < flip * (1 - flip_band) and s > pw * (1 + wall_room):
        return align_bonus, "below γ-flip, room to put wall ✓"
    if s > flip * (1 + flip_band):
        return -oppose_penalty, "above γ-flip ✗"
    return 0.0, "near γ-flip — momentum decides"


def compute_composite_score(row, spot, weights, dte=1, gex_balance=0.0, em_pct=0.0,
                            vanna_ex=0.0, charm_ex=0.0, max_vex=0.0, max_cex=0.0):
    """Composite 0-100 score; thin wrapper over score_components + weighted_score."""
    comps = score_components(row, spot, dte, gex_balance, em_pct,
                             vanna_ex, charm_ex, max_vex, max_cex)
    return weighted_score(comps, weights)
