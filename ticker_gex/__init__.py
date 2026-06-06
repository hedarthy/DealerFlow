"""On-demand, user-requested ticker dealerflow alert (standalone, self-contained).

A user requests any ticker from inside Discord (``/gex TSLA``) and gets back the same
five-message dealerflow construct the hourly SPY alert ships — a titled summary
magnet-table card, the Gamma / Vanna / Charm strike x expiry heatmaps, and a
front-expiry triptych — but for the requested symbol and rendered on demand.

This package is intentionally **self-contained**: it vendors its own copies of the
Black-Scholes exposure math, the CBOE/yfinance chain fetch, an Eastern-time helper and
the Discord poster so it runs entirely on its own and can never affect the repo's other
alerts/pipelines (the twice-daily watchlist screener and the hourly SPY alert). Nothing
here imports from those packages and nothing in them imports from here.
"""
