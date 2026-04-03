"""
Deterministic TA indicator computation via the `ta` library.

All indicators are computed here before any model call — models must never
infer or recall market facts, they reason only over this pre-computed data.
"""

import numpy as np
import pandas as pd
import ta.trend as trend
import ta.momentum as momentum
import ta.volatility as volatility

from fetcher.models import OHLCVBar, TechnicalIndicators


def compute(bars: list[OHLCVBar]) -> TechnicalIndicators:
    """Compute all indicators from a list of OHLCVBar (oldest-first)."""
    df = _to_df(bars)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # ── Trend ──────────────────────────────────────────────────────────────
    ema_8   = trend.ema_indicator(close, window=8)
    ema_21  = trend.ema_indicator(close, window=21)
    ema_50  = trend.ema_indicator(close, window=50)
    ema_200 = trend.ema_indicator(close, window=200)

    # ── Momentum ───────────────────────────────────────────────────────────
    rsi = momentum.rsi(close, window=14)

    macd_val  = trend.macd(close, window_slow=26, window_fast=12)
    macd_sig  = trend.macd_signal(close, window_slow=26, window_fast=12, window_sign=9)
    macd_hist = trend.macd_diff(close, window_slow=26, window_fast=12, window_sign=9)

    # ── Volatility ─────────────────────────────────────────────────────────
    bb_upper = volatility.bollinger_hband(close, window=20, window_dev=2)
    bb_mid   = volatility.bollinger_mavg(close, window=20)
    bb_lower = volatility.bollinger_lband(close, window=20, window_dev=2)

    atr = volatility.average_true_range(high, low, close, window=14)

    hv_20 = _historical_volatility(close, window=20)

    # ── Volume ─────────────────────────────────────────────────────────────
    vol_ma20 = volume.rolling(20).mean()
    volume_ratio = float(volume.iloc[-1] / vol_ma20.iloc[-1])

    # ── Derived signals ────────────────────────────────────────────────────
    # 3-bar change in MACD histogram (positive = rising momentum)
    macd_hist_slope = float(macd_hist.iloc[-1] - macd_hist.iloc[-4])

    # Price percentile within 90-day high-low range [0–1]
    recent_90 = df.tail(90)
    high_90 = float(recent_90["high"].max())
    low_90 = float(recent_90["low"].min())
    price_pct_90d = (
        float((close.iloc[-1] - low_90) / (high_90 - low_90))
        if high_90 != low_90
        else 0.5
    )

    # ── Support / Resistance (pivot-based, nearest within ±5% of close) ───
    support, resistance = _nearest_levels(df)

    return TechnicalIndicators(
        ema_8=_last(ema_8),
        ema_21=_last(ema_21),
        ema_50=_last(ema_50),
        ema_200=_last(ema_200),
        rsi_14=_last(rsi),
        macd=_last(macd_val),
        macd_signal=_last(macd_sig),
        macd_hist=_last(macd_hist),
        bb_upper=_last(bb_upper),
        bb_mid=_last(bb_mid),
        bb_lower=_last(bb_lower),
        atr_14=_last(atr),
        hv_20=hv_20,
        volume_ratio=volume_ratio,
        macd_hist_slope=macd_hist_slope,
        price_pct_90d=price_pct_90d,
        nearest_support=support,
        nearest_resistance=resistance,
    )


# ── Helpers ────────────────────────────────────────────────────────────────

def _to_df(bars: list[OHLCVBar]) -> pd.DataFrame:
    return pd.DataFrame([b.model_dump() for b in bars]).set_index("date")


def _last(series: pd.Series) -> float:
    val = series.iloc[-1]
    if pd.isna(val):
        raise ValueError(
            f"Indicator '{series.name}' is NaN at latest bar — insufficient history?"
        )
    return float(val)


def _historical_volatility(close: pd.Series, window: int = 20) -> float:
    """Annualised close-to-close HV as a decimal (e.g. 0.25 = 25%)."""
    log_returns = np.log(close / close.shift(1)).dropna()
    hv = log_returns.rolling(window).std().iloc[-1] * np.sqrt(252)
    return float(hv)


def _nearest_levels(
    df: pd.DataFrame,
    window: int = 10,
    band: float = 0.05,
) -> tuple[float | None, float | None]:
    """
    Pivot highs/lows over the last 90 bars; return nearest support (below close)
    and resistance (above close) within `band` (5%).
    """
    close = float(df["close"].iloc[-1])
    recent = df.tail(90)

    pivot_highs = recent["high"][
        recent["high"] == recent["high"].rolling(window, center=True).max()
    ].dropna().values.tolist()

    pivot_lows = recent["low"][
        recent["low"] == recent["low"].rolling(window, center=True).min()
    ].dropna().values.tolist()

    resistance_levels = [p for p in pivot_highs if close < p <= close * (1 + band)]
    support_levels    = [p for p in pivot_lows  if close * (1 - band) <= p < close]

    return (
        max(support_levels)    if support_levels    else None,
        min(resistance_levels) if resistance_levels else None,
    )
