def generate_strategy(contract, key_levels, regime, score):
    is_call = contract["type"] == "call"
    flip = key_levels["gamma_flip"]
    call_wall = key_levels["call_wall"]
    put_wall = key_levels["put_wall"]
    bullets = []
    if is_call and regime == "positive":
        entry = f"Buy on break above gamma flip ({flip:.2f}) with positive GEX confirmation"
    elif is_call and regime == "negative":
        entry = f"Buy on break above call wall ({call_wall}) in trending regime"
    else:
        entry = f"Buy on break below gamma flip ({flip:.2f}) with negative GEX confirmation"
    bullets.append(f"**Entry:** {entry}")
    target = call_wall if is_call else put_wall
    bullets.append(f"**Target:** {target} or 0.8–1.5% move (regime-supported)")
    stop = put_wall if is_call else call_wall
    bullets.append(f"**Stop:** Below {stop} – invalidates dealer positioning")
    return bullets
