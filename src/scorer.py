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


def vanna_directional_adjustment(opt_type, net_vex, gross_vex,
                                 align_bonus=8.0, oppose_penalty=8.0, min_frac=0.05):
    """Fold the SIGN of net dealer vanna into a directional read, as additive points.

    VEX = ∂(dealer delta)/∂σ with dealer sign calls +1 / puts −1. A POSITIVE net vanna
    means that when implied vol falls — the base case into a calm tape and a 0–2 DTE
    expiry — dealers must BUY the underlying (price support, a tailwind for calls); a
    NEGATIVE net is a tailwind for puts. This is the directional content of the
    ``vanna_regime`` the report already prints, now actually scored. The strength scales
    with how lopsided the book is (``|net| / gross``) so a balanced chain contributes
    nothing. It stays a modest overlay (not a core weight) because the sign of the hedge
    flips if IV *rises* instead — we don't predict IV direction.

    Returns ``(points, label)``: > 0 confirms, < 0 opposes, 0 neutral.
    """
    g = _num(gross_vex)
    if g <= 0:
        return 0.0, "vanna n/a"
    bal = _num(net_vex) / g  # net dealer vanna balance in [-1, 1]
    if abs(bal) < min_frac:
        return 0.0, "vanna balanced"
    is_call = str(opt_type).lower().startswith("c")
    strength = min(1.0, abs(bal))
    aligned = (bal > 0) == is_call  # +net vanna favors calls, −net favors puts
    sign_txt = "+net vanna" if bal > 0 else "−net vanna"
    if aligned:
        return align_bonus * strength, f"vanna tailwind ({sign_txt}) ✓"
    return -oppose_penalty * strength, f"vanna headwind ({sign_txt}) ✗"


def flow_imbalance_adjustment(opt_type, call_prem, put_prem,
                              align_bonus=8.0, oppose_penalty=8.0, min_frac=0.10):
    """Signed order-flow lean from the day's traded *premium* (vol × price), call vs put.

    A deliberately crude aggressor proxy: with no time-and-sales we can't separate buys
    from sells, but a heavy skew of the session's option $-premium into calls (or puts)
    is a bullish (bearish) lean. ``imbalance = (callP − putP)/(callP + putP)`` in
    [−1, 1]; under ``min_frac`` the tape is two-sided and ignored. Points scale with
    ``|imbalance|`` so a one-sided tape counts more than a marginal skew.

    Returns ``(points, label)``: > 0 confirms, < 0 opposes, 0 neutral.
    """
    cp, pp = max(0.0, _num(call_prem)), max(0.0, _num(put_prem))
    tot = cp + pp
    if tot <= 0:
        return 0.0, "flow n/a"
    imb = (cp - pp) / tot
    if abs(imb) < min_frac:
        return 0.0, "flow two-sided"
    is_call = str(opt_type).lower().startswith("c")
    strength = min(1.0, abs(imb))
    aligned = (imb > 0) == is_call  # call-heavy premium favors calls, put-heavy favors puts
    side = "calls" if imb > 0 else "puts"
    mark = "✓" if aligned else "✗"
    pts = align_bonus * strength if aligned else -oppose_penalty * strength
    return pts, f"flow {side}-led {abs(imb) * 100:.0f}% {mark}"


# Per-regime emphasis for the four directional overlays. A positive-gamma (pinning /
# mean-reverting) book makes the GEX structure and dealer-flow reads reliable while
# momentum chops, so EMA is down-weighted and structure up-weighted; a negative-gamma
# (trending) book has weak walls and accelerating price, so momentum/flow lead and the
# structure read is cut. Tunable from config (``conviction_regime_weights``).
DEFAULT_REGIME_WEIGHTS = {
    "positive": {"ema": 0.7, "gex": 1.2, "vanna": 1.0, "flow": 0.9},
    "negative": {"ema": 1.2, "gex": 0.5, "vanna": 1.0, "flow": 1.1},
}


def aggregate_conviction(pa_pts, gd_pts, vanna_pts, flow_pts, regime,
                         weights=None, adaptive=True):
    """Combine the four directional overlays into one regime-weighted conviction score.

    ``(pa_pts, gd_pts, vanna_pts, flow_pts)`` are the signed EMA / GEX-structure /
    vanna-sign / order-flow overlays. When ``adaptive`` they are weighted by the gamma
    regime (see ``DEFAULT_REGIME_WEIGHTS``) so the screen leans on whichever signals are
    trustworthy in that regime; otherwise every weight is 1.0. Alignment counts are taken
    on the *unweighted* signs (weighting never flips a sign) and drive the confluence gate
    in the agent.

    Returns ``(conviction, aligned_count, opposed_count)``.
    """
    signals = {"ema": _num(pa_pts), "gex": _num(gd_pts),
               "vanna": _num(vanna_pts), "flow": _num(flow_pts)}
    if adaptive:
        w = (weights or DEFAULT_REGIME_WEIGHTS).get(
            regime, DEFAULT_REGIME_WEIGHTS["negative"])
    else:
        w = {k: 1.0 for k in signals}
    conviction = sum(w.get(k, 1.0) * v for k, v in signals.items())
    aligned = sum(1 for v in signals.values() if v > 0)
    opposed = sum(1 for v in signals.values() if v < 0)
    return conviction, aligned, opposed


def compute_composite_score(row, spot, weights, dte=1, gex_balance=0.0, em_pct=0.0,
                            vanna_ex=0.0, charm_ex=0.0, max_vex=0.0, max_cex=0.0):
    """Composite 0-100 score; thin wrapper over score_components + weighted_score."""
    comps = score_components(row, spot, dte, gex_balance, em_pct,
                             vanna_ex, charm_ex, max_vex, max_cex)
    return weighted_score(comps, weights)
