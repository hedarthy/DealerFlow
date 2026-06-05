def generate_strategy(contract, key_levels, regime, score, vanna_regime="neutral"):
    is_call = contract["type"] == "call"
    flip = key_levels.get("gamma_flip", 0.0)
    call_wall = key_levels.get("call_wall", 0.0)
    put_wall = key_levels.get("put_wall", 0.0)

    def lvl(x):
        return f"{x:.2f}" if x else "n/a"

    bullets = []
    if is_call and regime == "positive":
        entry = f"Buy on break above gamma flip ({lvl(flip)}) with positive GEX confirmation"
    elif is_call and regime == "negative":
        entry = f"Buy on break above call wall ({lvl(call_wall)}) in trending regime"
    else:
        entry = f"Buy on break below gamma flip ({lvl(flip)}) with negative GEX confirmation"
    bullets.append(f"**Entry:** {entry}")
    target = call_wall if is_call else put_wall
    bullets.append(f"**Target:** {lvl(target)} or 0.8-1.5% move (regime-supported)")
    stop = put_wall if is_call else call_wall
    bullets.append(f"**Stop:** Below {lvl(stop)} - invalidates dealer positioning")

    if vanna_regime == "positive":
        bullets.append("**Vanna:** Net-positive dealer vanna - an IV drop forces dealer buying "
                       "(price support; tailwind for calls). Dealers sell into an IV pop.")
    elif vanna_regime == "negative":
        bullets.append("**Vanna:** Net-negative dealer vanna - an IV drop forces dealer selling "
                       "(price pressure; headwind for calls). Dealers buy into an IV pop.")
    bullets.append("**Charm:** Delta decay accelerates into expiry/EOD - dealer re-hedging "
                   "favours decisive intraday continuation")

    pa_label = contract.get("pa_label")
    ema8, ema21 = contract.get("ema8"), contract.get("ema21")
    if pa_label and pa_label not in ("n/a", "EMAs mixed"):
        if ema8 and ema21:
            bullets.append(f"**Price-Action ({pa_label}):** spot vs 8EMA {ema8:.2f} / 21EMA "
                           f"{ema21:.2f} — aligned with the SeanTrades momentum stack")
        else:
            bullets.append(f"**Price-Action:** {pa_label} — aligned with the SeanTrades momentum stack")
    elif pa_label == "EMAs mixed":
        bullets.append("**Price-Action:** price is tangled in the 8/21 EMAs — no momentum "
                       "confirmation, size down")
    return bullets
