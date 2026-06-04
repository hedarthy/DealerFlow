def compute_composite_score(row, gex_grid, spot, regime, weights):
    gex_score = 30 if regime == "positive" else 10
    flow_proxy = min(25, (row.get("volume", 0) / row.get("openInterest", 1)) * 5)
    squeeze = 20 if row.get("openInterest", 0) > 1000 else 5
    vanna_charm = 15
    moneyness_dte = max(0, 10 - abs(row["strike"] - spot) / spot * 100)
    score = (weights["gex_regime"] * gex_score +
             weights["flow_proxy"] * flow_proxy +
             weights["squeeze"] * squeeze +
             weights["vanna_charm"] * vanna_charm +
             weights["moneyness_dte"] * moneyness_dte)
    return min(100, max(0, score))
