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


def compute_composite_score(row, spot, weights, dte=1, gex_balance=0.0, em_pct=0.0,
                            vanna_ex=0.0, charm_ex=0.0, max_vex=0.0, max_cex=0.0):
    """Composite 0-100 score; thin wrapper over score_components + weighted_score."""
    comps = score_components(row, spot, dte, gex_balance, em_pct,
                             vanna_ex, charm_ex, max_vex, max_cex)
    return weighted_score(comps, weights)
