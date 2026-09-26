"""
Black-Scholes pricing for vertical debit spreads.

There is no free long-run history of option quotes, so the backtest prices
spreads from the underlying with an IV proxy (HV-20 × markup). This gets the
shape of the payoff and time decay right; it does NOT capture skew, IV crush or
real bid/ask — those are approximated by the markup and a fixed slippage.
"""

import math
from typing import Literal

Direction = Literal["bull", "bear"]

_RISK_FREE = 0.04


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, sigma: float, kind: str,
             r: float = _RISK_FREE, q: float = 0.0) -> float:
    """European option price with dividend yield q. Collapses to intrinsic value at or past expiry."""
    if strike <= 0:  # a wide bear spread on a very low-priced underlying
        return spot - strike * math.exp(-r * max(t_years, 0.0)) if kind == "call" else 0.0
    if t_years <= 0 or sigma <= 0:
        return max(spot - strike, 0.0) if kind == "call" else max(strike - spot, 0.0)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc = strike * math.exp(-r * t_years)
    fwd = spot * math.exp(-q * t_years)
    if kind == "call":
        return fwd * _norm_cdf(d1) - disc * _norm_cdf(d2)
    return disc * _norm_cdf(-d2) - fwd * _norm_cdf(-d1)


def bs_put_delta(spot: float, strike: float, t_years: float, sigma: float,
                 r: float = _RISK_FREE, q: float = 0.0) -> float:
    """Put delta (negative). Used to pick short strikes by delta, as traders quote them."""
    if t_years <= 0 or sigma <= 0:
        return -1.0 if spot < strike else 0.0
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    return -math.exp(-q * t_years) * _norm_cdf(-d1)


def spread_value(direction: Direction, spot: float, long_strike: float, width: float,
                 t_years: float, sigma: float) -> float:
    """Value of one bull call spread (long K, short K+w) or bear put spread (long K, short K-w)."""
    if direction == "bull":
        return (bs_price(spot, long_strike, t_years, sigma, "call")
                - bs_price(spot, long_strike + width, t_years, sigma, "call"))
    return (bs_price(spot, long_strike, t_years, sigma, "put")
            - bs_price(spot, long_strike - width, t_years, sigma, "put"))
