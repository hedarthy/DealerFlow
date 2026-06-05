def generate_strategy(contract, key_levels, regime, score, vanna_regime="neutral"):
    is_call = contract["type"] == "call"
    spot = contract.get("spot", 0.0) or 0.0
    em = contract.get("em_pct", 0.0) or 0.0       # ATM expected move %, e.g. 1.2
    flip = key_levels.get("gamma_flip", 0.0) or 0.0
    call_wall = key_levels.get("call_wall", 0.0) or 0.0
    put_wall = key_levels.get("put_wall", 0.0) or 0.0

    def rel(x):
        """A level shown as price + signed distance from the current spot."""
        if not x:
            return "n/a"
        if not spot:
            return f"${x:.2f}"
        return f"${x:.2f} ({(x / spot - 1) * 100:+.1f}%)"

    # Without a spot we cannot anchor anything — degrade rather than emit a $0 plan.
    if not spot:
        return ["**Plan:** insufficient data — no spot price to anchor entry/exit."]

    em = max(em, 0.8)               # floor the expected move so targets aren't trivially tight
    momentum = (regime == "negative")
    trigger = call_wall if is_call else put_wall   # the breakout level in momentum regime

    # --- Entry, anchored to where price is right now ---
    if is_call:
        if momentum:
            entry = (f"momentum — long only on a break and hold above {rel(trigger)}" if trigger
                     else "momentum — long only on a confirmed breakout (no call wall computed)")
        elif call_wall and spot >= call_wall:
            entry = f"don't chase — spot ${spot:.2f} is at the call wall {rel(call_wall)}; wait for a pullback that holds"
        elif flip and spot <= flip:
            entry = f"stand aside — below the gamma flip {rel(flip)}; wait for a reclaim before going long"
        else:
            entry = f"long near ${spot:.2f} — above the flip; buy dips, scale toward the call wall {rel(call_wall)}"
    else:
        if momentum:
            entry = (f"momentum — short only on a break and hold below {rel(trigger)}" if trigger
                     else "momentum — short only on a confirmed breakdown (no put wall computed)")
        elif put_wall and spot <= put_wall:
            entry = f"don't chase — spot ${spot:.2f} is at the put wall {rel(put_wall)}; wait for a failed bounce"
        elif flip and spot >= flip:
            entry = f"stand aside — above the gamma flip {rel(flip)}; wait for a loss before going short"
        else:
            entry = f"short near ${spot:.2f} — below the flip; sell rips, scale toward the put wall {rel(put_wall)}"

    # --- Target ---
    # Positive (mean-reversion) regime: the same-side wall is a magnet to scale into.
    # Negative (momentum) regime: the wall is just the breakout trigger, so project ~one
    # expected move *beyond* it rather than stalling at it.
    if momentum:
        base = trigger if trigger else spot
        tgt = base * (1 + em / 100) if is_call else base * (1 - em / 100)
        target = f"{rel(tgt)} ≈ one move past the trigger — trend, trail it"
    else:
        tgt_is_wall = bool(call_wall and call_wall > spot) if is_call else bool(put_wall and put_wall < spot)
        if tgt_is_wall:
            tgt = call_wall if is_call else put_wall
            target = f"{rel(tgt)} {'call' if is_call else 'put'}-wall magnet — scale out into it"
        else:
            tgt = spot * (1 + em / 100) if is_call else spot * (1 - em / 100)
            target = f"{rel(tgt)} ≈ one expected move — trail the move"

    # --- Stop: the NEAREST invalidation level on the risk side of spot ---
    if is_call:
        below = [lv for lv in (flip, put_wall) if lv and lv < spot]
        stop = max(below) if below else 0.0
        kind = "the gamma flip" if (stop and stop == flip) else "dealer support"
    else:
        above = [lv for lv in (flip, call_wall) if lv and lv > spot]
        stop = min(above) if above else 0.0
        kind = "the gamma flip" if (stop and stop == flip) else "dealer resistance"
    stop_txt = (f"{rel(stop)} — loss of {kind}" if stop
                else f"a decisive move back through the gamma flip {rel(flip)}")

    bullets = [
        f"**Entry:** {entry}",
        f"**Target:** {target}",
        f"**Stop:** {stop_txt}",
    ]

    # --- R/R: only when the geometry is valid (target & stop on the correct sides) ---
    geometry_ok = bool(stop and tgt and (
        (is_call and tgt > spot and stop < spot) or
        (not is_call and tgt < spot and stop > spot)))
    if geometry_ok:
        reward = abs(tgt / spot - 1) * 100
        risk = abs(stop / spot - 1) * 100
        if risk > 0:
            rr = reward / risk
            tail = "" if rr >= 1.5 else " — thin, size down"
            bullets.append(f"**R/R:** ≈{rr:.1f}:1 ({reward:.1f}% to target vs {risk:.1f}% to stop){tail}")
    return bullets
