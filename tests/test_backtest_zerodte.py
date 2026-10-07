"""
Tests for backtest/zerodte.py — trade rules and bankroll simulation on synthetic
minute bars; no API calls.
"""

import random
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

import backtest.zerodte as z
from backtest.zerodte import (ET, STRIKE_GRIDS, Rule, Trade, bankroll, cache_dir, equity_path,
                              load_day, load_days, occ, parse_occ, signal_overlap, simulate_day)

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


# ── multi-ticker handling ──────────────────────────────────────────────────


def test_occ_symbol_other_tickers_and_half_strikes():
    assert occ(DAY, "C", 480, "QQQ") == "QQQ240301C00480000"
    assert occ(DAY, "P", 201.5, "IWM") == "IWM240301P00201500"


@pytest.mark.parametrize("sym", ["SPY240301C00502000", "IWM240301P00201500", "SPXW240301C05100000"])
def test_parse_occ_round_trips_any_root_length(sym):
    root, kind, strike = parse_occ(sym)
    assert occ(DAY, kind, strike, root) == sym


def test_cache_dir_keeps_legacy_spy_path_and_nests_others(monkeypatch, tmp_path):
    monkeypatch.setattr(z, "_CACHE", tmp_path / "zerodte")
    assert cache_dir("SPY") == tmp_path / "zerodte"
    assert cache_dir("spy") == tmp_path / "zerodte"
    assert cache_dir("qqq") == tmp_path / "zerodte" / "QQQ"
    assert cache_dir("IWM") == tmp_path / "zerodte" / "IWM"


def test_spy_strike_grid_unchanged_and_iwm_uses_half_dollars():
    # the SPY cache was built with round(price) ± 12 in $1 steps; refetches must match it
    assert STRIKE_GRIDS["SPY"].strikes(500.4) == [float(s) for s in range(488, 513)]
    iwm = STRIKE_GRIDS["IWM"].strikes(201.3)
    assert 201.5 in iwm and 201.0 in iwm and iwm[1] - iwm[0] == 0.5


def _write_day(path, ticker, with_options=True):
    rows = [{"symbol": ticker, "timestamp": "2024-03-01 14:30:00+00:00", "open": 1, "high": 1,
             "low": 1, "close": 1, "volume": 1}]
    if with_options:
        rows.append({**rows[0], "symbol": occ(DAY, "C", 480, ticker)})
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def test_load_day_splits_out_the_requested_underlying(tmp_path):
    _write_day(tmp_path / "2024-03-01.csv", "QQQ")
    stock, opts = load_day(tmp_path / "2024-03-01.csv", "QQQ")
    assert len(stock) == 1 and list(opts) == [occ(DAY, "C", 480, "QQQ")]


def test_load_days_skips_days_without_same_day_options(monkeypatch, tmp_path):
    monkeypatch.setattr(z, "_CACHE", tmp_path)
    _write_day(tmp_path / "IWM" / "2024-03-01.csv", "IWM", with_options=False)
    _write_day(tmp_path / "IWM" / "2024-03-04.csv", "IWM")
    _write_day(tmp_path / "2024-03-01.csv", "SPY")      # SPY's cache must not leak in
    cov = load_days("iwm")
    assert [d for d, _, _ in cov.days] == [date(2024, 3, 4)]
    assert cov.uncovered == [date(2024, 3, 1)]
    assert "1 without (skipped)" in cov.report()


def test_simulate_day_works_for_other_roots():
    opts = {occ(DAY, "C", 502, "QQQ"): _option({time(9, 30): 0.25})}
    t = simulate_day(DAY, _spy(501.0), opts, Rule("orb", 0.25, None), 0.01, RNG)
    assert t.symbol == "QQQ240301C00502000"


def test_equity_path_tracks_drawdown_and_unaffordable_trades():
    trades = [Trade(DAY + timedelta(1), "x", 1.00, 0.0, False),  # 2 contracts lost → 1000
              Trade(DAY, "x", 1.00, 2.00, True),                 # first by date: 2 × +100 → 1200
              Trade(DAY + timedelta(2), "x", 20.0, 40.0, True)]  # $2,000 contract, unaffordable
    eq = equity_path(trades, 0.2)
    assert eq.final == pytest.approx(1000)
    assert eq.max_dd == pytest.approx(200 / 1200)
    assert eq.skipped == 1


def test_signal_overlap_counts_same_direction():
    d1, d2, d3 = DAY, DAY + timedelta(1), DAY + timedelta(2)
    lines = "\n".join(signal_overlap({
        "SPY": {d1: "C", d2: "P", d3: None},
        "QQQ": {d1: "C", d2: "C", d3: None},
        "IWM": {d1: "C", d2: None, d3: "P"},
    }))
    assert "≥2 signal the same day: 2 days; same direction: 1" in lines
    assert "all 3 signal the same day: 1 days; same direction: 1" in lines
    assert "SPY/QQQ: both signal 2 of 2 days either does, same direction 1/2" in lines
