def _num(x, default=0.0):
    """Coerce to float, mapping None/non-numeric/NaN to a default."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # v != v is True only for NaN


def compute_composite_score(row, gex_grid, spot, regime, weights):
    oi = _num(row.get("openInterest", 0))
    vol = _num(row.get("volume", 0))
    strike = _num(row.get("strike", spot), spot)
    # Each component is scaled 0-100; weights sum to 1.0, so the result is 0-100.
    gex_score = 100 if regime == "positive" else 40
    flow_proxy = min(100, (vol / max(oi, 1)) * 20)
    squeeze = 100 if oi > 1000 else 25
    vanna_charm = 60
    moneyness_dte = max(0, 100 - abs(strike - spot) / spot * 100 * 10)
    score = (weights["gex_regime"] * gex_score +
             weights["flow_proxy"] * flow_proxy +
             weights["squeeze"] * squeeze +
             weights["vanna_charm"] * vanna_charm +
             weights["moneyness_dte"] * moneyness_dte)
    return float(min(100, max(0, score)))
