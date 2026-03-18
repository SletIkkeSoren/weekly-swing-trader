"""
Pydantic models for the fetcher output payload.

The consensus engine receives a list of MarketSnapshot objects — one per ticker.
LLMs must never receive raw P&L history; they receive OHLCV + pre-computed indicators only.
"""

from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, Field


class OHLCVBar(BaseModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None


class TechnicalIndicators(BaseModel):
    # Trend
    ema_8: float
    ema_21: float
    ema_50: float
    ema_200: float

    # Momentum
    rsi_14: float
    macd: float
    macd_signal: float
    macd_hist: float

    # Volatility
    bb_upper: float = Field(description="Bollinger Band upper (20, 2σ)")
    bb_mid: float
    bb_lower: float
    atr_14: float
    hv_20: float = Field(description="20-day historical volatility (annualised, decimal)")

    # Volume
    volume_ratio: float = Field(
        description="Latest volume / 20-day average volume"
    )

    # Key levels (nearest S/R within ±5% of close)
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None


class OpenPosition(BaseModel):
    """An open option position held on the account."""
    ticker: str               # underlying, e.g. "TSLA"
    occ_symbol: str           # full OCC symbol, e.g. "TSLA240119C00250000"
    option_type: str          # "call" or "put"
    qty: int
    avg_entry_price: float
    current_price: float
    pnl_pct: float            # decimal: -0.55 = down 55%, 1.50 = up 150%
    days_to_expiry: int


class MarketSnapshot(BaseModel):
    ticker: str
    as_of: datetime = Field(description="UTC timestamp of when data was fetched")
    latest_close: float
    bars: list[OHLCVBar] = Field(description="Daily bars, oldest-first, full lookback")
    indicators: TechnicalIndicators
    open_positions: list[OpenPosition] = Field(
        default_factory=list,
        description="Open option positions for this ticker at time of fetch",
    )
