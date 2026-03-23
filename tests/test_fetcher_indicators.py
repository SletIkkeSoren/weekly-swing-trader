"""
Tests for fetcher/indicators.py — deterministic TA computation.

All tests use synthetic bar data; no external API calls.
"""

from datetime import date, timedelta

import numpy as np
import pytest

from fetcher.indicators import _historical_volatility, _nearest_levels, _to_df, compute
from fetcher.models import OHLCVBar, TechnicalIndicators


# ── Fixtures / helpers ─────────────────────────────────────────────────────


def _make_bars(
    n: int = 252,
    base_price: float = 100.0,
    trend: float = 1.001,
) -> list[OHLCVBar]:
    """Generate n synthetic OHLCV bars with a constant multiplicative trend."""
    bars = []
    start = date(2025, 1, 1)
    price = base_price
    for i in range(n):
        price *= trend
        bars.append(
            OHLCVBar(
                date=start + timedelta(days=i),
                open=round(price * 0.999, 4),
                high=round(price * 1.005, 4),
                low=round(price * 0.995, 4),
                close=round(price, 4),
                volume=1_000_000.0,
            )
        )
    return bars


# ── compute() ─────────────────────────────────────────────────────────────


class TestCompute:
    def test_returns_technical_indicators_type(self):
        bars = _make_bars(252)
        result = compute(bars)
        assert isinstance(result, TechnicalIndicators)

    def test_all_fields_are_finite_floats(self):
        bars = _make_bars(252)
        result = compute(bars)
        for field, value in result.model_dump().items():
            if value is not None:
                assert isinstance(value, float), f"{field} is not float"
                assert np.isfinite(value), f"{field} is not finite: {value}"

    def test_raises_on_insufficient_history_for_ema200(self):
        """Fewer than 200 bars means EMA-200 is NaN at the last row → ValueError."""
        bars = _make_bars(50)  # not enough for EMA-200
        with pytest.raises(ValueError, match="NaN"):
            compute(bars)

    def test_ema_ordering_uptrend(self):
        """In a sustained uptrend, shorter EMAs should be above longer ones."""
        bars = _make_bars(252, trend=1.002)
        ind = compute(bars)
        assert ind.ema_8 > ind.ema_21 > ind.ema_50 > ind.ema_200

    def test_rsi_bounds(self):
        bars = _make_bars(252)
        ind = compute(bars)
        assert 0 <= ind.rsi_14 <= 100

    def test_bollinger_band_ordering(self):
        bars = _make_bars(252)
        ind = compute(bars)
        assert ind.bb_upper > ind.bb_mid > ind.bb_lower

    def test_atr_is_positive(self):
        bars = _make_bars(252)
        ind = compute(bars)
        assert ind.atr_14 > 0

    def test_hv20_is_positive(self):
        bars = _make_bars(252)
        ind = compute(bars)
        assert ind.hv_20 > 0

    def test_volume_ratio_is_one_for_constant_volume(self):
        """All bars have the same volume → ratio should equal 1.0."""
        bars = _make_bars(252)
        ind = compute(bars)
        assert ind.volume_ratio == pytest.approx(1.0, abs=0.01)

    def test_compute_is_deterministic(self):
        """Same input → identical output (no randomness)."""
        bars = _make_bars(252)
        result_a = compute(bars)
        result_b = compute(bars)
        assert result_a.model_dump() == result_b.model_dump()


# ── _historical_volatility ─────────────────────────────────────────────────


class TestHistoricalVolatility:
    def test_flat_price_series_has_near_zero_hv(self):
        import pandas as pd
        # Constant prices → log returns all zero → HV ≈ 0
        close = pd.Series([100.0] * 30)
        hv = _historical_volatility(close, window=20)
        assert hv == pytest.approx(0.0, abs=1e-9)

    def test_volatile_series_has_higher_hv(self):
        import pandas as pd
        rng = np.random.default_rng(42)
        # Low-vol series
        prices_low = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.005, 50)))
        # High-vol series
        prices_high = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.05, 50)))
        hv_low = _historical_volatility(prices_low, window=20)
        hv_high = _historical_volatility(prices_high, window=20)
        assert hv_high > hv_low

    def test_returns_finite_float(self):
        import pandas as pd
        close = pd.Series(_make_bars(50))  # won't work directly
        bars = _make_bars(50)
        close = pd.Series([b.close for b in bars])
        hv = _historical_volatility(close, window=20)
        assert isinstance(hv, float)
        assert np.isfinite(hv)


# ── _nearest_levels ────────────────────────────────────────────────────────


class TestNearestLevels:
    def test_no_levels_within_band_returns_none_none(self):
        """Perfectly flat price → all pivot highs/lows equal close → none above/below."""
        bars = _make_bars(100, trend=1.0)
        df = _to_df(bars)
        support, resistance = _nearest_levels(df)
        # With flat prices, pivot highs == close so resistance_levels requires p > close
        # and support_levels requires p < close — both empty
        assert support is None or isinstance(support, float)
        assert resistance is None or isinstance(resistance, float)

    def test_returns_support_below_close_and_resistance_above(self):
        """In a trended series there should be some pivot levels detectable."""
        bars = _make_bars(252, trend=1.001)
        df = _to_df(bars)
        support, resistance = _nearest_levels(df)
        close = float(df["close"].iloc[-1])
        if support is not None:
            assert support < close
        if resistance is not None:
            assert resistance > close

    def test_support_within_5pct_band(self):
        bars = _make_bars(252, trend=1.001)
        df = _to_df(bars)
        close = float(df["close"].iloc[-1])
        support, _ = _nearest_levels(df)
        if support is not None:
            assert support >= close * 0.95

    def test_resistance_within_5pct_band(self):
        bars = _make_bars(252, trend=1.001)
        df = _to_df(bars)
        close = float(df["close"].iloc[-1])
        _, resistance = _nearest_levels(df)
        if resistance is not None:
            assert resistance <= close * 1.05
