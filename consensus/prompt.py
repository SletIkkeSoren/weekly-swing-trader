"""
Builds the prompt sent to every model.

Rules enforced here:
- Models receive OHLCV + computed indicators only (no P&L history)
- Models are explicitly told NOT to recall market facts from training
- Output schema is injected so the model can return structured JSON
"""

import json
from datetime import date, timedelta

from fetcher.models import MarketSnapshot

SYSTEM_PROMPT = """You are a quantitative options analyst. You will be given market data
for a single equity: recent OHLCV bars and pre-computed technical indicators.

RULES:
- Reason ONLY from the data provided. Do not recall or infer any market facts from
  your training data (earnings dates, analyst ratings, news, etc.).
- Your entire response must be a single valid JSON object matching the schema below.
- If the data is insufficient or ambiguous, set action to "HOLD".

OUTPUT SCHEMA (respond with nothing but this JSON):
{
  "action": "BUY_CALL" | "BUY_PUT" | "HOLD",
  "strike": <float — nearest $1 increment to your target, null if HOLD>,
  "expiry": "<YYYY-MM-DD — nearest or next Friday, null if HOLD>",
  "confidence": <float 0.0–1.0>,
  "reasoning": ["<concise point>", ...],
  "invalidating_conditions": ["<condition that would make this trade wrong>", ...]
}
"""


def next_friday(from_date: date | None = None) -> date:
    d = from_date or date.today()
    days_ahead = 4 - d.weekday()  # Friday = 4
    if days_ahead <= 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)


def build_user_message(snapshot: MarketSnapshot) -> str:
    ind = snapshot.indicators
    recent_bars = snapshot.bars[-20:]  # last 20 trading days; indicators cover the rest

    bars_table = "\n".join(
        f"  {b.date}  O:{b.open:.2f}  H:{b.high:.2f}  L:{b.low:.2f}"
        f"  C:{b.close:.2f}  V:{b.volume:.0f}"
        for b in recent_bars
    )

    nearest_friday = next_friday()
    following_friday = next_friday(nearest_friday + timedelta(days=1))

    return f"""
TICKER: {snapshot.ticker}
AS OF:  {snapshot.as_of.date()}
CLOSE:  {snapshot.latest_close:.2f}

--- RECENT PRICE ACTION (last 20 sessions) ---
{bars_table}

--- TECHNICAL INDICATORS ---
Trend
  EMA-8:   {ind.ema_8:.2f}   EMA-21:  {ind.ema_21:.2f}
  EMA-50:  {ind.ema_50:.2f}  EMA-200: {ind.ema_200:.2f}

Momentum
  RSI-14:  {ind.rsi_14:.1f}
  MACD:    {ind.macd:.4f}  Signal: {ind.macd_signal:.4f}  Hist: {ind.macd_hist:.4f}

Volatility
  BB-Upper: {ind.bb_upper:.2f}  BB-Mid: {ind.bb_mid:.2f}  BB-Lower: {ind.bb_lower:.2f}
  ATR-14:   {ind.atr_14:.2f}
  HV-20:    {ind.hv_20 * 100:.1f}%  (annualised)

Volume
  Volume ratio (vs 20d avg): {ind.volume_ratio:.2f}x

Key Levels
  Nearest support:    {f"{ind.nearest_support:.2f}" if ind.nearest_support else "none within 5%"}
  Nearest resistance: {f"{ind.nearest_resistance:.2f}" if ind.nearest_resistance else "none within 5%"}

--- AVAILABLE EXPIRIES ---
Nearest Friday:  {nearest_friday}
Following Friday: {following_friday}

Respond with valid JSON only.
""".strip()
