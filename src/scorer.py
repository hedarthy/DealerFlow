def _num(x, default=0.0):
    """Coerce to float, mapping None/non-numeric/NaN to a default."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # v != v is True only for NaN


def compute_composite_score(row, spot, regime, weights,
                            vanna_ex=0.0, charm_ex=0.0, max_vex=0.0, max_cex=0.0):
    """Composite 0-100 score. Each component is 0-100 and weights sum to 1.0.

    ``vanna_charm`` is now a real, data-driven component: the net dealer-signed
    vanna and charm exposure *at the contract's strike*, normalised against the
    largest such net strike exposure in the chain. Strikes where dealer vanna/charm
    concentrates (typically near ATM and into expiry) score higher — which is
    exactly where reflexive dealer hedging moves price on 0-2 DTE trades.
    """
    oi = _num(row.get("openInterest", 0))
    vol = _num(row.get("volume", 0))
    strike = _num(row.get("strike", spot), spot)
    gex_score = 100 if regime == "positive" else 40
    flow_proxy = min(100, (vol / max(oi, 1)) * 20)
    squeeze = 100 if oi > 1000 else 25
    v = (abs(vanna_ex) / max_vex) if max_vex else 0.0
    c = (abs(charm_ex) / max_cex) if max_cex else 0.0
    vanna_charm = min(100.0, 100.0 * (0.6 * v + 0.4 * c))
    moneyness_dte = max(0, 100 - abs(strike - spot) / spot * 100 * 10)
    score = (weights["gex_regime"] * gex_score +
             weights["flow_proxy"] * flow_proxy +
             weights["squeeze"] * squeeze +
             weights["vanna_charm"] * vanna_charm +
             weights["moneyness_dte"] * moneyness_dte)
    return float(min(100, max(0, score)))
