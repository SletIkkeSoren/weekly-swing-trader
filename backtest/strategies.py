"""
Deterministic entry signals, evaluated on the close of each bar.

Each strategy returns a Series aligned to the input index: +1 bullish, -1 bearish,
0 nothing. A signal on bar i may only use data up to and including bar i — the
engine enters on bar i+1's open, mirroring the pre-open pipeline.
"""

from typing import Callable

import pandas as pd
import ta.momentum as momentum
import ta.trend as trend


def _combine(bull: pd.Series, bear: pd.Series) -> pd.Series:
    return bull.astype(int) - bear.astype(int)


def rsi2_pullback(df: pd.DataFrame) -> pd.Series:
    """Short-term oversold dip inside a long-term uptrend (and the mirror)."""
    close = df["close"]
    sma200 = close.rolling(200).mean()
    rsi2 = momentum.rsi(close, window=2)
    return _combine((close > sma200) & (rsi2 < 10), (close < sma200) & (rsi2 > 90))


def donchian_breakout(df: pd.DataFrame) -> pd.Series:
    """Close through the prior 20-day high/low, with the 200-day trend."""
    close = df["close"]
    sma200 = close.rolling(200).mean()
    prior_high = df["high"].rolling(20).max().shift(1)
    prior_low = df["low"].rolling(20).min().shift(1)
    return _combine((close > sma200) & (close > prior_high), (close < sma200) & (close < prior_low))


def ema_pullback(df: pd.DataFrame) -> pd.Series:
    """Stacked EMAs; bar tags the 21-EMA and closes back on the trend side."""
    close = df["close"]
    e21 = trend.ema_indicator(close, window=21)
    e50 = trend.ema_indicator(close, window=50)
    e200 = trend.ema_indicator(close, window=200)
    bull = (e21 > e50) & (e50 > e200) & (df["low"] <= e21) & (close > e21)
    bear = (e21 < e50) & (e50 < e200) & (df["high"] >= e21) & (close < e21)
    return _combine(bull, bear)


def every_day(df: pd.DataFrame) -> pd.Series:
    """Baseline: a bullish signal on every bar. The edge of any strategy is measured against this."""
    return pd.Series(1, index=df.index).where(df["close"].rolling(200).mean().notna(), 0)


STRATEGIES: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "rsi2_pullback": rsi2_pullback,
    "donchian_breakout": donchian_breakout,
    "ema_pullback": ema_pullback,
}
