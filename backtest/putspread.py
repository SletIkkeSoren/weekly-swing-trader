"""
SPY put credit spreads, sized as a percentage of current equity (compounding).

Sells the volatility risk premium: SPY implied vol usually exceeds the vol that
follows. Unlike backtest/engine.py, options here are priced from the market's
own implied vol — VIX — not from a realized-vol guess, so the premium is
measured, not assumed:

    IV(K) = VIX − atm_discount + skew × (% out of the money)

VIX sits a little above at-the-money IV (it weights the wings), and OTM puts
trade above ATM — `skew` in vol points per 1% OTM. Rates from 13-week T-bills.

Timing mirrors the live pipeline: decisions on the close, fills at next open.
Sizing: a new spread may lose at most `risk_per_trade` × equity; all open
spreads together at most `max_open_risk` × equity. As equity grows the bot
trades wider/more spreads, as it shrinks it trades fewer — both compound.

    python -m backtest.putspread [--risk 0.10] [--skew 0.8] [--stop 2] [--trend-filter]
"""

import argparse
import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from backtest.data import load_bars
from backtest.pricing import bs_price, bs_put_delta

START = "1993-01-01"
PERIODS = {
    "1993-2004 (pre-SPY options, SPX-equivalent)": (date(1993, 2, 1), date(2004, 12, 31)),
    "2005-2019": (date(2005, 1, 1), date(2019, 12, 31)),
    "2020-now": (date(2020, 1, 1), date(2100, 1, 1)),
    "full 1993-now": (date(1993, 2, 1), date(2100, 1, 1)),
}


@dataclass
class Params:
    dte: int = 45
    exit_dte: int = 21
    short_delta: float = 0.20
    take_profit: float = 0.5        # close once half the credit is captured
    stop_mult: float | None = None  # close if cost-to-close reaches credit × this
    risk_per_trade: float = 0.10
    max_open_risk: float = 0.50
    target_width_pct: float = 0.01  # preferred width ≈ 1% of spot, narrowed to fit the budget
    slippage: float = 0.05          # $ per spread per side (mleg order)
    skew: float = 0.8               # IV vol points per 1% OTM
    atm_discount: float = 1.5       # VIX minus this ≈ ATM IV, in vol points
    div_yield: float = 0.015
    trend_filter: bool = False      # only open new spreads while SPY > 200-day SMA


@dataclass
class Position:
    short_k: float
    long_k: float
    expiry: pd.Timestamp
    contracts: int
    credit: float                   # per share, net of slippage
    opened: pd.Timestamp

    @property
    def width(self) -> float:
        return self.short_k - self.long_k

    @property
    def max_loss(self) -> float:
        return (self.width - self.credit) * 100 * self.contracts


@dataclass
class Result:
    equity: pd.Series
    trades: list[float] = field(default_factory=list)   # P&L as % of equity at entry
    worst_trade: float = 0.0


def _iv(strike: float, spot: float, vix: float, p: Params) -> float:
    otm_pct = max(0.0, (spot - strike) / spot * 100)
    return max(vix - p.atm_discount + p.skew * otm_pct, 5.0) / 100


def _put(spot: float, k: float, t: float, vix: float, r: float, p: Params) -> float:
    return bs_price(spot, k, t, _iv(k, spot, vix, p), "put", r, p.div_yield)


def spread_mid(spot: float, pos_short: float, pos_long: float, t: float, vix: float, r: float,
               p: Params) -> float:
    return _put(spot, pos_short, t, vix, r, p) - _put(spot, pos_long, t, vix, r, p)


def load_market() -> pd.DataFrame:
    spy = load_bars("SPY", START, adjusted=False)
    vix = load_bars("^VIX", START)["close"].rename("vix")
    irx = load_bars("^IRX", START)["close"].rename("irx")
    df = spy.join([vix, irx], how="left")
    df[["vix", "irx"]] = df[["vix", "irx"]].ffill()
    df["sma200"] = df["close"].rolling(200).mean()
    return df.dropna(subset=["vix", "irx"])


def open_spread(day: pd.Timestamp, spot: float, vix: float, r: float, equity: float,
                open_risk: float, p: Params) -> Position | None:
    t = p.dte / 365
    budget = min(equity * p.risk_per_trade, equity * p.max_open_risk - open_risk)
    if budget <= 0:
        return None

    short_k = math.floor(spot)
    while short_k > spot * 0.5 and abs(bs_put_delta(spot, short_k, t, _iv(short_k, spot, vix, p), r,
                                                    p.div_yield)) > p.short_delta:
        short_k -= 1

    # Preferred width, narrowed in $1 steps until one contract fits the budget
    width = max(1, round(spot * p.target_width_pct))
    while width >= 1:
        credit = spread_mid(spot, short_k, short_k - width, t, vix, r, p) - p.slippage
        loss_one = (width - credit) * 100
        if credit > 0.05 and loss_one <= budget:
            n = int(budget // loss_one)
            return Position(short_k, short_k - width, day + pd.Timedelta(days=p.dte), n, credit, day)
        width -= 1
    return None


def simulate(df: pd.DataFrame, lo: date, hi: date, p: Params, start_equity: float = 1000.0) -> Result:
    df = df[(df.index >= pd.Timestamp(lo)) & (df.index <= pd.Timestamp(hi))]
    cash = start_equity
    positions: list[Position] = []
    to_close: list[Position] = []
    want_open = False
    equity_at_open: dict[int, float] = {}
    res_trades: list[float] = []
    curve: list[float] = []
    prev_week = None

    for day, row in df.iterrows():
        r = row["irx"] / 100

        # ── Open: execute yesterday's decisions ───────────────────────────
        for pos in to_close:
            t = max((pos.expiry - day).days, 0) / 365
            cost = min(spread_mid(row["open"], pos.short_k, pos.long_k, t, row["vix"], r, p) + p.slippage,
                       pos.width)
            cash -= cost * 100 * pos.contracts
            pnl = (pos.credit - cost) * 100 * pos.contracts
            res_trades.append(pnl / equity_at_open.pop(id(pos)))
            positions.remove(pos)
        to_close = []

        if want_open:
            marked = cash - sum(
                spread_mid(row["open"], x.short_k, x.long_k, (x.expiry - day).days / 365, row["vix"], r, p)
                * 100 * x.contracts for x in positions)
            pos = open_spread(day, row["open"], row["vix"], r, marked,
                              sum(x.max_loss for x in positions), p)
            if pos:
                cash += pos.credit * 100 * pos.contracts
                positions.append(pos)
                equity_at_open[id(pos)] = marked
        want_open = False

        # ── Close: mark to market, decide exits and entries for tomorrow ──
        liab = 0.0
        for pos in positions:
            remaining = (pos.expiry - day).days
            mid = spread_mid(row["close"], pos.short_k, pos.long_k, remaining / 365, row["vix"], r, p)
            liab += mid * 100 * pos.contracts
            if (mid <= pos.credit * (1 - p.take_profit) or remaining <= p.exit_dte
                    or (p.stop_mult and mid >= pos.credit * p.stop_mult)):
                to_close.append(pos)
        equity = cash - liab
        curve.append(equity)
        if equity <= 0:
            break

        week = day.isocalendar()[1]
        if week != prev_week:       # one new spread per week, decided on the week's first close
            trend_ok = not p.trend_filter or (row["close"] > row["sma200"])
            want_open = trend_ok
            prev_week = week

    eq = pd.Series(curve, index=df.index[:len(curve)])
    return Result(eq, res_trades, min(res_trades) if res_trades else 0.0)


def metrics(eq: pd.Series) -> tuple[float, float, float]:
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    daily = eq.pct_change().dropna()
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    sharpe = daily.mean() / daily.std() * math.sqrt(252) if daily.std() > 0 else 0.0
    return cagr, float(-(eq / eq.cummax() - 1).min()), sharpe


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk", type=float, default=0.10, help="max loss per new spread, fraction of equity")
    ap.add_argument("--max-open-risk", type=float, default=0.50)
    ap.add_argument("--delta", type=float, default=0.20)
    ap.add_argument("--skew", type=float, default=0.8)
    ap.add_argument("--slippage", type=float, default=0.05)
    ap.add_argument("--stop", type=float, default=None, help="stop at credit × N")
    ap.add_argument("--trend-filter", action="store_true")
    ap.add_argument("--yearly", action="store_true")
    ap.add_argument("--equity", type=float, default=1000.0, help="starting account size")
    a = ap.parse_args()
    p = Params(risk_per_trade=a.risk, max_open_risk=a.max_open_risk, short_delta=a.delta, skew=a.skew,
               slippage=a.slippage, stop_mult=a.stop, trend_filter=a.trend_filter)

    df = load_market()
    spy_tr = load_bars("SPY", START)["close"]   # dividend-adjusted, for buy-and-hold
    print(f"\n{p}\n")
    print(f"  {'period':44} {'':6} {'CAGR':>7} {'maxDD':>6} {'Sharpe':>6} {'end $':>10} "
          f"{'trades':>6} {'win':>4} {'worst':>6}")
    for label, (lo, hi) in PERIODS.items():
        res = simulate(df, lo, hi, p, a.equity)
        c, dd, sh = metrics(res.equity)
        wins = sum(x > 0 for x in res.trades) / len(res.trades) if res.trades else 0
        print(f"  {label:44} {'spread':6} {c:+7.1%} {dd:6.1%} {sh:6.2f} {res.equity.iloc[-1]:>10,.0f} "
              f"{len(res.trades):6} {wins:4.0%} {res.worst_trade:+6.1%}")
        spy = spy_tr[(spy_tr.index >= pd.Timestamp(lo)) & (spy_tr.index <= pd.Timestamp(hi))]
        c, dd, sh = metrics(spy / spy.iloc[0] * a.equity)
        print(f"  {'':44} {'SPY':6} {c:+7.1%} {dd:6.1%} {sh:6.2f} {spy.iloc[-1] / spy.iloc[0] * a.equity:>10,.0f}")

    if a.yearly:
        res = simulate(df, *PERIODS["full 1993-now"], p, a.equity)
        yr_s = res.equity.groupby(res.equity.index.year).last().pct_change()
        spy_y = spy_tr.groupby(spy_tr.index.year).last().pct_change()
        print("\n  year  spread     SPY")
        for y in yr_s.index[1:]:
            print(f"  {y}  {yr_s[y]:+7.1%} {spy_y.get(y, np.nan):+7.1%}")


if __name__ == "__main__":
    main()
