"""
Tests for backtest/pricing.py — the Black-Scholes spread pricer every backtest
number depends on.
"""

import math

import pytest

from backtest.pricing import bs_price, spread_value


def test_put_call_parity():
    s, k, t, sig, r = 100.0, 105.0, 30 / 365, 0.25, 0.04
    lhs = bs_price(s, k, t, sig, "call") - bs_price(s, k, t, sig, "put")
    assert lhs == pytest.approx(s - k * math.exp(-r * t), abs=1e-9)


def test_intrinsic_at_expiry():
    assert bs_price(110, 100, 0, 0.3, "call") == 10
    assert bs_price(110, 100, 0, 0.3, "put") == 0


@pytest.mark.parametrize("direction", ["bull", "bear"])
@pytest.mark.parametrize("spot", [80.0, 100.0, 120.0])
def test_spread_value_bounded_by_width(direction, spot):
    v = spread_value(direction, spot, 100.0, 5.0, 30 / 365, 0.3)
    assert 0.0 <= v <= 5.0


def test_atm_spread_costs_about_half_width():
    v = spread_value("bull", 100.0, 100.0, 1.0, 30 / 365, 0.2)
    assert 0.4 < v < 0.6


def test_nonpositive_strike_put_is_worthless():
    assert bs_price(1.0, 0.0, 30 / 365, 0.5, "put") == 0.0
