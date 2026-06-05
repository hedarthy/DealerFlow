def generate_strategy(contract, key_levels, regime, score):
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
    return bullets
