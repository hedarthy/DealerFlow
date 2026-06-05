"""Optional CBOE delayed-quotes source: real exchange OI / IV / greeks per contract.

CBOE publishes free (~15-min delayed) option chains carrying REAL open interest,
implied vol and exchange greeks (gamma/delta/theta/vega/rho). Those are strictly
better dealer-positioning inputs than yfinance, which exposes no greeks and whose
OI/IV are frequently stale or missing. We still recompute gamma/vanna/charm via
Black-Scholes (vanna and charm aren't in the feed), but feeding the BS model CBOE's
real IV and pairing it with CBOE's real OI yields far more trustworthy GEX/VEX/CEX.

``fetch_cboe`` returns ``(spot, {expiry: DataFrame})`` in the exact column shape the
rest of the pipeline already expects from yfinance, or ``None`` on any failure so the
caller can fall back to yfinance.
"""
import requests
import pandas as pd

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _parse_occ(sym):
    """OCC option symbol -> (expiry 'YYYY-MM-DD', opt_type, strike).

    The final 15 chars are YYMMDD(6) + C/P(1) + strike(8, in thousandths);
    anything before that is the (variable-length) root.
    """
    tail = sym[-15:]
    yy, mm, dd = tail[0:2], tail[2:4], tail[4:6]
    cp = tail[6]
    strike = int(tail[7:]) / 1000.0
    expiry = f"20{yy}-{mm}-{dd}"
    opt_type = "call" if cp == "C" else "put"
    return expiry, opt_type, strike


def _num(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default


def fetch_cboe(ticker, timeout=20):
    """Return (spot, {expiry: DataFrame}) from CBOE, or None on any failure."""
    try:
        r = requests.get(CBOE_URL.format(symbol=ticker.upper()), headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return None
        payload = r.json().get("data", {}) or {}
        options = payload.get("options") or []
        spot = _num(payload.get("current_price"))
        if spot <= 0 or not options:
            return None
        rows = []
        for o in options:
            sym = str(o.get("option", ""))
            if len(sym) < 15:
                continue
            try:
                expiry, opt_type, strike = _parse_occ(sym)
            except (ValueError, IndexError):
                continue
            last = _num(o.get("last_trade_price"))
            if last <= 0:
                bid, ask = _num(o.get("bid")), _num(o.get("ask"))
                if bid or ask:
                    last = (bid + ask) / 2.0
            rows.append({
                "expiry": expiry,
                "opt_type": opt_type,
                "strike": strike,
                "openInterest": _num(o.get("open_interest")),
                "impliedVolatility": _num(o.get("iv")),
                "volume": _num(o.get("volume")),
                "lastPrice": last,
                "contractSymbol": sym,
                "cboe_gamma": _num(o.get("gamma")),
            })
        if not rows:
            return None
        df = pd.DataFrame(rows)
        chains = {exp: g.drop(columns="expiry").reset_index(drop=True)
                  for exp, g in df.groupby("expiry")}
        return spot, chains
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
