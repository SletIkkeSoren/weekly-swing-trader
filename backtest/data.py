"""
Historical daily bars for backtesting, cached to disk.

Uses Yahoo Finance (via yfinance) rather than Alpaca: no credentials needed and
the history goes back well before Alpaca's IEX feed starts. Research-only — the
live pipeline still gets its bars from Alpaca (fetcher/).
"""

import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("backtest.data")

_CACHE_DIR = Path(os.getenv("BACKTEST_CACHE", ".cache/backtest"))


def load_bars(ticker: str, start: str = "2009-01-01", refresh: bool = False,
              adjusted: bool = True) -> pd.DataFrame:
    """Daily bars with columns open/high/low/close/volume.

    `adjusted=False` keeps traded prices (split- but not dividend-adjusted) — what
    option strikes are quoted against. Index tickers (^VIX, ^IRX) have no volume.
    """
    path = _CACHE_DIR / f"{ticker}{'' if adjusted else '_raw'}_{start}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, index_col=0, parse_dates=True)

    import yfinance as yf  # research-only dependency, see requirements-backtest.txt

    df = yf.Ticker(ticker).history(start=start, auto_adjust=adjusted)
    if df.empty:
        raise ValueError(f"No bar data returned for {ticker}")
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
    log.info("Cached %d bars for %s", len(df), ticker)
    return df


def historical_volatility(close: pd.Series, window: int = 20) -> pd.Series:
    """Annualised close-to-close volatility — same definition as fetcher/indicators.py."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(window).std() * np.sqrt(252)
