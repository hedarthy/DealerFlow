"""On-demand engine: turn a user-requested ticker into the 5-message dealerflow post.

``run_for_ticker(ticker, webhook_url=None, out_dir=None)`` validates the symbol, pulls
its option chain (CBOE -> yfinance), builds the dealer-signed GEX/VEX/CEX grids for the
five nearest expiries over a 25-up/25-down strike window, renders the five Skylit-style
images, and (when a webhook is provided) posts them as five Discord messages. It is
written to **never raise** on a bad/illiquid ticker: every failure path returns a
structured :class:`TickerGexResult` with a friendly ``message`` instead.

Posting semantics are explicit: ``webhook_url=None`` means *do not post* (render only).
Callers that want a live post resolve the webhook from the environment and pass it in.

Rendering uses matplotlib's global pyplot state, which is not thread-safe; since the bot
runs this in a thread-pool executor with limited concurrency, all rendering is serialised
behind a module-level lock.
"""
import argparse
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

from ticker_gex import agent, data_source, notify
from ticker_gex.calendar_util import eastern_now
from ticker_gex.exposure import (
    compute_exposure_grids, get_key_levels, get_regime, get_vanna_regime,
    select_window_strikes,
)

# Load the repo-root .env (if present) without changing the working directory, so a local
# run can pick up TICKER_GEX_DISCORD_WEBHOOK the same way the other alerts read their env.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PKG_DIR)
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

EXPECTED_POSTS = 5
_TICKER_RE = re.compile(r"^[A-Z][A-Z.]{0,5}$")  # 1-6 chars, letters/dots, leading letter

# matplotlib/pyplot global state is not thread-safe; serialise all rendering.
_RENDER_LOCK = threading.Lock()


@dataclass
class TickerGexResult:
    """Structured outcome of a single on-demand request."""
    ok: bool
    ticker: str
    source: Optional[str] = None
    images: List[str] = field(default_factory=list)
    posted: int = 0
    expected_posts: int = EXPECTED_POSTS
    phase: str = "init"
    partial: bool = False
    error: Optional[str] = None
    message: str = ""


def validate_ticker(raw):
    """Normalise + validate a user-supplied ticker.

    Returns ``(ticker, None)`` on success or ``(None, reason)`` on failure. Whitespace is
    stripped and the symbol upper-cased, so ``" tsla "`` -> ``"TSLA"``. Accepts 1-6
    letters with optional dots (e.g. ``BRK.B``); rejects empty, over-long or junk input.
    """
    if raw is None:
        return None, "no ticker provided"
    sym = str(raw).strip().upper().lstrip("$")
    if not sym:
        return None, "no ticker provided"
    if not _TICKER_RE.match(sym):
        return None, (f"`{raw}` isn't a valid ticker — use 1-6 letters, e.g. `SPY`, "
                      f"`QQQ`, `TSLA`.")
    return sym, None


def _post(content, png, webhook_url, tries=3):
    """``send_discord`` with light retry/backoff (Discord 429 / transient 5xx)."""
    for i in range(tries):
        try:
            notify.send_discord(content, png, webhook_url=webhook_url)
            return
        except Exception as e:  # noqa: BLE001 - retry any transient post failure
            if i == tries - 1:
                raise
            wait = 3 * (i + 1)
            print(f"WARNING: Discord post failed ({e}); retrying in {wait}s")
            time.sleep(wait)


def _build_rows_and_grids(chains, expiries, spot):
    """Compute per-expiry exposure grids + summary rows. Returns (rows, per_exp, labels,
    all_strikes)."""
    rows = []
    per_exp = {"gex": {}, "vex": {}, "cex": {}}
    exp_labels = []
    all_strikes = set()
    for exp, dte in expiries:
        df = chains[exp]
        gex, vex, cex = compute_exposure_grids(df, spot, exp)
        if not gex:
            continue
        keys = get_key_levels(gex, spot)
        regime = get_regime(gex)
        vanna_regime = get_vanna_regime(vex)
        col = f"{exp[5:]}\nD{dte}"   # column header: MM-DD over its DTE
        exp_labels.append(col)
        per_exp["gex"][col] = gex
        per_exp["vex"][col] = vex
        per_exp["cex"][col] = cex
        all_strikes.update(gex.keys())
        rows.append({
            "exp": exp, "dte": dte, "regime": regime, "vanna_regime": vanna_regime,
            "keys": keys,
            "flip_s": (f"${keys['gamma_flip']:.0f}" if keys["gamma_flip"] else "n/a"),
            "cw_s": (f"${keys['call_wall']:.0f}" if keys["call_wall"] else "n/a"),
            "pw_s": (f"${keys['put_wall']:.0f}" if keys["put_wall"] else "n/a"),
            "net_gex": sum(gex.values()) / 1e3,
            "net_vex": sum(vex.values()) / 1e3,
            "net_cex": sum(cex.values()) / 1e3,
        })
    return rows, per_exp, exp_labels, all_strikes


def _grids_meta(ticker):
    """Per-greek (key, panel name, colorbar unit, filename suffix, decimals, caption)."""
    return [
        ("gex", "Gamma (GEX)", "$K per 1% spot", "gamma", 1,
         f"\U0001f7e2 **{ticker} Gamma (GEX)** \u2014 dealer gamma by strike \u00d7 expiry. "
         "Positive (bright) rows are call-heavy pin/resistance magnets; negative (dark) "
         "rows accelerate moves. The King \u2605 is the dominant strike on the board."),
        ("vex", "Vanna (VEX)", "$K per 1.00\u03c3", "vanna", 1,
         f"\U0001f7e3 **{ticker} Vanna (VEX)** \u2014 how dealer hedging shifts when IV "
         "moves. Bright rows draw price on a vol drop / supportive flows; dark rows "
         "pressure price as vol rises. King \u2605 = largest vanna magnet."),
        ("cex", "Charm (CEX)", "$K per day", "charm", 1,
         f"\U0001f7e0 **{ticker} Charm (CEX)** \u2014 delta decay into expiry (time-of-day "
         "drift). Bright rows pull price up as charm hedging buys; dark rows bleed it "
         "lower. Strongest near expiry. King \u2605 = dominant charm strike."),
    ]


def _render_all(ticker, spot, source, et, rows, per_exp, exp_labels, window, out_dir):
    """Render the 5 PNGs (summary card + 3 grids + front triptych) into ``out_dir``.

    Returns ``[(caption, path), ...]`` in post order. All matplotlib work runs under the
    render lock so concurrent on-demand requests can't corrupt shared pyplot state.
    """
    stamp = et.strftime("%Y%m%d_%H%M%S")
    prefix = f"{ticker}_{stamp}"

    def art(suffix):
        return os.path.join(out_dir, f"{prefix}_{suffix}.png")

    label = f"{et:%H:%M} ET"
    dates = " / ".join(r["exp"] for r in rows)
    base = f"{ticker} \u00b7 spot ${spot:.2f} \u00b7 {label} \u00b7 {dates}"

    messages = []
    with _RENDER_LOCK:
        try:
            # 1) Summary magnet-table card + its caption.
            summary_text = agent.build_summary_text(ticker, spot, source, et, rows)
            table_png = art("summary")
            agent.render_summary_table(rows, table_png, ticker, et, spot, slot=None)
            messages.append((summary_text, table_png))

            # 2-4) Gamma / Vanna / Charm strike x expiry heatmaps.
            for key, name, unit, suffix, dec, caption in _grids_meta(ticker):
                mat = agent.build_greek_matrix(per_exp[key], window, exp_labels)
                path = art(suffix)
                agent.render_grid(mat, spot, f"{name} \u2014 {base}", unit, path, decimals=dec)
                messages.append((caption, path))

            # 5) Front-expiry triptych (nearest expiry's 3 greeks side by side).
            front_label = exp_labels[0]
            front = rows[0]
            tri_png = art("front_triptych")
            agent.render_front_triptych(
                per_exp, window, front_label, spot,
                f"{ticker} Front Expiry {front['exp']} (D{front['dte']}) \u2014 "
                f"Gamma \u00b7 Vanna \u00b7 Charm \u00b7 spot ${spot:.2f} \u00b7 {label}",
                tri_png,
            )
            messages.append((
                f"\U0001f9f2 **{ticker} Front Expiry {front['exp']} (D{front['dte']})** \u2014 "
                "Gamma \u00b7 Vanna \u00b7 Charm side by side. Same strikes, independent colour "
                "scales; the white line is spot and each panel stars its own King \u2605.",
                tri_png,
            ))
        finally:
            # Defensively drop any figures left open by a mid-render error.
            import matplotlib.pyplot as plt
            plt.close("all")
    return messages


def run_for_ticker(ticker, webhook_url=None, out_dir=None):
    """Render (and optionally post) the 5-message dealerflow construct for ``ticker``.

    ``webhook_url=None`` renders only and posts nothing. Always returns a
    :class:`TickerGexResult`; never raises for a bad/illiquid ticker.
    """
    phase = "validate"
    sym, err = validate_ticker(ticker)
    if err:
        return TickerGexResult(ok=False, ticker=str(ticker), phase=phase,
                               error=err, message=err)

    owns_dir = out_dir is None
    if owns_dir:
        out_dir = tempfile.mkdtemp(prefix=f"ticker_gex_{sym}_")
    else:
        os.makedirs(out_dir, exist_ok=True)

    posted = 0
    images: List[str] = []
    source = None
    try:
        phase = "fetch"
        got = data_source.get_chains(sym)
        if not got:
            msg = (f"Couldn't pull an option chain for **{sym}** — it may not be optionable "
                   f"or the data feeds are unavailable right now.")
            return TickerGexResult(ok=False, ticker=sym, phase=phase, error="no_data",
                                   message=msg)
        spot, chains, source = got

        et = eastern_now()
        expiries = agent.select_expiries(chains, et)
        if not expiries:
            msg = f"No expirations on/after today for **{sym}** to chart."
            return TickerGexResult(ok=False, ticker=sym, source=source, phase=phase,
                                   error="no_expiries", message=msg)

        phase = "compute"
        rows, per_exp, exp_labels, all_strikes = _build_rows_and_grids(chains, expiries, spot)
        if not rows:
            msg = f"No open interest in **{sym}**'s near expiries — nothing to chart."
            return TickerGexResult(ok=False, ticker=sym, source=source, phase=phase,
                                   error="no_open_interest", message=msg)

        window = select_window_strikes(all_strikes, spot, agent.WINDOW_STRIKES)

        phase = "render"
        messages = _render_all(sym, spot, source, et, rows, per_exp, exp_labels,
                               window, out_dir)
        images = [p for _, p in messages]

        if not webhook_url:
            return TickerGexResult(
                ok=True, ticker=sym, source=source, images=images, posted=0,
                phase="render",
                message=(f"Rendered {len(images)} {sym} dealerflow images "
                         f"(no webhook supplied; nothing posted)."))

        phase = "post"
        for caption, png in messages:
            _post(caption, png, webhook_url)
            posted += 1
            time.sleep(1)

        return TickerGexResult(
            ok=True, ticker=sym, source=source, images=images, posted=posted,
            phase="done",
            message=f"Posted {posted} {sym} dealerflow messages (source {source}).")
    except Exception as e:  # noqa: BLE001 - never crash the caller on a bad ticker
        partial = posted > 0
        if partial:
            msg = (f"Posted {posted}/{EXPECTED_POSTS} {sym} messages, then hit an error "
                   f"while {phase}: {e}")
        else:
            msg = f"Failed to build the {sym} dealerflow ({phase}): {e}"
        return TickerGexResult(ok=False, ticker=sym, source=source, images=images,
                               posted=posted, phase=phase, partial=partial,
                               error=str(e), message=msg)
    finally:
        if owns_dir and not webhook_url:
            # Render-only temp dirs are the caller's to inspect; keep them. Posted runs
            # in a caller-managed dir clean themselves elsewhere. (No-op placeholder kept
            # intentionally simple — the bot wraps this in its own TemporaryDirectory.)
            pass


def _main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render/post the on-demand dealerflow construct for one ticker.")
    parser.add_argument("--ticker", required=True, help="Ticker symbol, e.g. TSLA")
    parser.add_argument("--out-dir", default=None,
                        help="Directory for the rendered PNGs (default: a temp dir).")
    parser.add_argument("--no-post", action="store_true",
                        help="Render only; never post to Discord even if a webhook is set.")
    args = parser.parse_args(argv)

    webhook = None if args.no_post else notify.resolve_webhook()
    if not args.no_post and not webhook:
        print("WARNING: no webhook resolved (set TICKER_GEX_DISCORD_WEBHOOK); rendering only.")

    result = run_for_ticker(args.ticker, webhook_url=webhook, out_dir=args.out_dir)
    print(("OK: " if result.ok else "ERROR: ") + result.message)
    if result.images:
        print("Images:\n  " + "\n  ".join(result.images))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(_main())
