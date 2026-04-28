"""
Builds the prompt sent to every model.

Rules enforced here:
- Models receive OHLCV + computed indicators only (no P&L history)
- Models are explicitly told NOT to recall market facts from training
- Output schema is injected so the model can return structured JSON
"""

import json
from datetime import date, timedelta

from fetcher.models import MarketSnapshot, TechnicalIndicators

SYSTEM_PROMPT = """You are a quantitative options analyst. You will be given market data
for a single equity: recent OHLCV bars, pre-computed technical indicators, and any
open option positions currently held.

RULES:
- Reason ONLY from the data provided. Do not recall or infer any market facts from
  your training data (earnings dates, analyst ratings, news, etc.).
- Your entire response must be a single valid JSON object matching the schema below.
- You MUST express a directional view (BUY_CALL or BUY_PUT) if any edge is visible in the data, even a modest one. Use confidence to quantify your conviction — low confidence is fine, the downstream system will filter weak signals. HOLD is reserved for truly indeterminate data where no directional bias can be identified at all.
- If open positions are shown, you must evaluate whether the underlying technical
  thesis is still intact. Small P&L swings (−5% to −20%) are normal option noise
  and are NOT a reason to close. This strategy targets 50–150% gains; premature
  exits destroy the edge. Only set action to "CLOSE" when there is a clear,
  confirmed technical reversal: trend direction has flipped (e.g. EMA crossover
  against the position, RSI reversing from extreme), a key support/resistance
  level has definitively broken, or momentum indicators confirm the move is over.
  When in doubt, output HOLD. The hard risk exits (stop-loss, take-profit,
  near-expiry) are handled by a separate system — you do not need to manage them.

OUTPUT SCHEMA (respond with nothing but this JSON):
{
  "action": "BUY_CALL" | "BUY_PUT" | "CLOSE" | "HOLD",
  "strike": <float — nearest $1 increment to your target, null if CLOSE or HOLD>,
  "expiry": "<YYYY-MM-DD — nearest or next Friday, null if CLOSE or HOLD>",
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


def third_friday(year: int, month: int) -> date:
    """Return the 3rd Friday of the given month (standard monthly options expiry)."""
    d = date(year, month, 1)
    days_until_friday = (4 - d.weekday()) % 7
    first_friday = d + timedelta(days=days_until_friday)
    return first_friday + timedelta(weeks=2)


def _rsi_label(rsi: float) -> str:
    if rsi < 30:
        return "OVERSOLD"
    elif rsi > 70:
        return "OVERBOUGHT"
    elif rsi < 45:
        return "WEAK"
    elif rsi > 55:
        return "STRONG"
    return "NEUTRAL"


def _ema_alignment_label(close: float, ind: TechnicalIndicators) -> str:
    emas = [ind.ema_8, ind.ema_21, ind.ema_50, ind.ema_200]
    if all(emas[i] > emas[i + 1] for i in range(3)):
        stack = "fully stacked bullish (8>21>50>200)"
    elif all(emas[i] < emas[i + 1] for i in range(3)):
        stack = "fully stacked bearish (8<21<50<200)"
    else:
        order = []
        pairs = [("8", "21"), ("21", "50"), ("50", "200")]
        for (a_name, b_name), (a_val, b_val) in zip(pairs, zip(emas, emas[1:])):
            order.append(f"{a_name}>{ b_name}" if a_val > b_val else f"{a_name}<{b_name}")
        stack = f"mixed ({', '.join(order)})"

    pct = (close - ind.ema_200) / ind.ema_200 * 100
    direction = "above" if pct >= 0 else "below"
    return f"{stack} | price {direction} EMA-200 by {abs(pct):.1f}%"


def _bb_position_label(close: float, ind: TechnicalIndicators) -> tuple[float, str]:
    """Returns (bb_pct 0-100, descriptive label)."""
    band_width = ind.bb_upper - ind.bb_lower
    bb_pct = (close - ind.bb_lower) / band_width * 100 if band_width else 50.0
    if bb_pct >= 80:
        label = "near upper band — extended"
    elif bb_pct <= 20:
        label = "near lower band — compressed"
    else:
        label = "mid-range"
    return bb_pct, label


def _macd_slope_label(slope: float) -> str:
    return "RISING" if slope > 0 else "FALLING" if slope < 0 else "FLAT"


def build_user_message(snapshot: MarketSnapshot) -> str:
    ind = snapshot.indicators
    close = snapshot.latest_close
    has_positions = bool(snapshot.open_positions)

    # ── Available expiries ────────────────────────────────────────────────
    today = date.today()
    nearest_friday = next_friday(today)
    following_friday = next_friday(nearest_friday + timedelta(days=1))
    monthly = third_friday(today.year, today.month)
    if monthly <= today:
        nm = (today.replace(day=1) + timedelta(days=32))
        monthly = third_friday(nm.year, nm.month)

    # ── Pre-interpreted signal labels ─────────────────────────────────────
    rsi_label = _rsi_label(ind.rsi_14)
    ema_label = _ema_alignment_label(close, ind)
    bb_pct, bb_label = _bb_position_label(close, ind)
    macd_slope_label = _macd_slope_label(ind.macd_hist_slope)
    price_pct_label = f"{ind.price_pct_90d * 100:.0f}th percentile of 90-day range"

    # ── Open positions section ────────────────────────────────────────────
    positions_section = ""
    if has_positions:
        lines = []
        for pos in snapshot.open_positions:
            pnl_sign = "+" if pos.pnl_pct >= 0 else ""
            lines.append(
                f"  {pos.occ_symbol} — {pos.qty} contract(s) @ ${pos.avg_entry_price:.2f} avg\n"
                f"  Current: ${pos.current_price:.2f} ({pnl_sign}{pos.pnl_pct:.0%}) | "
                f"Days to expiry: {pos.days_to_expiry}"
            )
        positions_section = (
            "\n--- OPEN POSITIONS ---\n"
            + "\n".join(lines)
            + "\n\n"
            "ACTION CONSTRAINT: A position is already held. "
            "You must output CLOSE or HOLD only — do not output BUY_CALL or BUY_PUT.\n"
        )

    # ── Recent price action — always included so model can assess thesis ─
    price_action_section = ""
    if True:
        recent_bars = snapshot.bars[-20:]
        bars_table = "\n".join(
            f"  {b.date}  O:{b.open:.2f}  H:{b.high:.2f}  L:{b.low:.2f}"
            f"  C:{b.close:.2f}  V:{b.volume:.0f}"
            + (f"  VWAP:{b.vwap:.2f}" if b.vwap is not None else "")
            for b in recent_bars
        )
        price_action_section = f"\n--- RECENT PRICE ACTION (last 20 sessions) ---\n{bars_table}\n"

    return f"""
TICKER: {snapshot.ticker}
AS OF:  {snapshot.as_of.date()}
CLOSE:  {close:.2f}
{positions_section}{price_action_section}
--- TECHNICAL INDICATORS ---
Trend
  EMA alignment: {ema_label}
  EMA-8:   {ind.ema_8:.2f}   EMA-21:  {ind.ema_21:.2f}
  EMA-50:  {ind.ema_50:.2f}  EMA-200: {ind.ema_200:.2f}

Momentum
  RSI-14:  {ind.rsi_14:.1f}  [{rsi_label}]
  MACD:    {ind.macd:.4f}  Signal: {ind.macd_signal:.4f}  Hist: {ind.macd_hist:.4f}  Slope: {ind.macd_hist_slope:+.4f} [{macd_slope_label}]

Volatility
  BB-Upper: {ind.bb_upper:.2f}  BB-Mid: {ind.bb_mid:.2f}  BB-Lower: {ind.bb_lower:.2f}
  BB position: {bb_pct:.0f}% ({bb_label})
  ATR-14:   {ind.atr_14:.2f}
  HV-20:    {ind.hv_20 * 100:.1f}%  (annualised)

Volume
  Volume ratio (vs 20d avg): {ind.volume_ratio:.2f}x

Price Context
  90-day range position: {price_pct_label}

Key Levels
  Nearest support:    {f"{ind.nearest_support:.2f}" if ind.nearest_support else "none within 5%"}
  Nearest resistance: {f"{ind.nearest_resistance:.2f}" if ind.nearest_resistance else "none within 5%"}

--- AVAILABLE EXPIRIES ---
Nearest Friday:   {nearest_friday}
Following Friday: {following_friday}
Monthly (3rd Fri): {monthly}

Respond with valid JSON only.
""".strip()
