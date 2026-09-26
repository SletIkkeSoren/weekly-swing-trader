"""
Tests for backtest/zerodte.py — trade rules and bankroll simulation on synthetic
minute bars; no API calls.
"""

import random
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from backtest.zerodte import ET, Rule, Trade, bankroll, occ, simulate_day

DAY = date(2024, 3, 1)


def _bars(prices: list[tuple[time, float]], high_bump: float = 0.0) -> pd.DataFrame:
    rows = []
    for t, p in prices:
        ts = datetime.combine(DAY, t, ET)
        rows.append({"timestamp": pd.Timestamp(ts), "t": t, "open": p, "high": p + high_bump,
                     "low": p, "close": p, "volume": 100})
    return pd.DataFrame(rows)


def _minutes(start: time, end: time) -> list[time]:
    out, cur = [], datetime.combine(DAY, start)
    while cur.time() <= end:
        out.append(cur.time())
        cur += timedelta(minutes=1)
    return out


def _spy(breakout_to: float) -> pd.DataFrame:
    """Opening range 500±0.5 until 9:44, then drifts to `breakout_to` by 9:59."""
    prices = []
    for t in _minutes(time(9, 30), time(15, 59)):
        prices.append((t, 500.0 if t < time(9, 45) else breakout_to))
    df = _bars(prices)
    df.loc[df["t"] < time(9, 45), "high"] = 500.5
    df.loc[df["t"] < time(9, 45), "low"] = 499.5
    return df


def _option(path: dict[time, float]) -> pd.DataFrame:
    """Piecewise-constant option price; keys are the times the price changes."""
    keys = sorted(path)
    prices = []
    for t in _minutes(time(9, 30), time(15, 59)):
        current = [k for k in keys if k <= t]
        prices.append((t, path[current[-1]] if current else path[keys[0]]))
    return _bars(prices)


RNG = random.Random(0)


def test_occ_symbol_format():
    assert occ(DAY, "C", 502) == "SPY240301C00502000"


def test_orb_breakout_buys_call_closest_to_target_premium_and_hits_take_profit():
    opts = {
        occ(DAY, "C", 502): _option({time(9, 30): 0.50, time(11, 0): 1.50}),
        occ(DAY, "C", 504): _option({time(9, 30): 0.20, time(11, 0): 0.70}),
        occ(DAY, "P", 499): _option({time(9, 30): 0.20}),
    }
    t = simulate_day(DAY, _spy(501.0), opts, Rule("orb", 0.25, 3.0), 0.01, RNG)
    assert t.symbol == occ(DAY, "C", 504)
    assert t.cost == pytest.approx(0.21)
    assert t.hit_tp and t.exit == pytest.approx(0.63)


def test_no_breakout_means_no_trade():
    opts = {occ(DAY, "C", 502): _option({time(9, 30): 0.30})}
    assert simulate_day(DAY, _spy(500.2), opts, Rule("orb", 0.25, 3.0), 0.01, RNG) is None


def test_time_exit_sells_at_cutoff_price_not_later():
    opts = {occ(DAY, "P", 497): _option({time(9, 30): 0.30, time(15, 0): 0.10, time(15, 45): 5.00})}
    t = simulate_day(DAY, _spy(499.0), opts, Rule("orb", 0.25, 3.0), 0.01, RNG)
    assert not t.hit_tp
    assert t.exit == pytest.approx(0.09)


def test_itm_contracts_are_not_candidates():
    opts = {occ(DAY, "C", 500): _option({time(9, 30): 0.25})}  # strike below 501 → ITM call
    assert simulate_day(DAY, _spy(501.0), opts, Rule("orb", 0.25, 3.0), 0.01, RNG) is None


def test_bankroll_all_winners_reaches_target_all_losers_ruin():
    win = [Trade(DAY, "x", 0.50, 1.50, True)]
    lose = [Trade(DAY, "x", 0.50, 0.0, False)]
    assert bankroll(win, 0.5, 20, paths=50).p_target == 1.0
    assert bankroll(lose, 0.5, 50, paths=50).p_ruin == 1.0
