"""Standalone SPY hourly dealerflow GEX/VEX/CEX heatmap alert.

This package is intentionally **self-contained**: it vendors its own copies of the
Black-Scholes exposure math, the CBOE/yfinance chain fetch, the NYSE calendar and the
Discord poster so it can run entirely on its own and can never affect the repo's other
alerts/pipelines (the twice-daily watchlist screener in ``src/``). Nothing here imports
from ``src/`` and nothing in ``src/`` imports from here.
"""
