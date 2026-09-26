"""
Backtest entrypoint: do any deterministic setups, traded as debit spreads on a
$1,000 account, beat both an unconditioned baseline and SPY buy-and-hold?

Reports in-sample (2010–2019) and out-of-sample (2020→) separately — only a
strategy that holds up in both is worth wiring into the live pipeline.

    pip install -r requirements-backtest.txt
    python -m backtest.main [--budget 50] [--no-stop]
"""

import argparse
import logging
from datetime import date

import pandas as pd

from backtest.data import load_bars
from backtest.engine import SpreadParams, Trade, prepare, run_strategy, simulate_portfolio, summarize
from backtest.strategies import STRATEGIES, every_day

ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLI", "XLY",
        "XLP", "XLU", "SMH", "GLD", "TLT", "EEM"]
# The current live universe. Picked in hindsight — survivorship bias flatters bullish setups.
STOCKS = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL"]

PERIODS = {
    "2010-2019 (in-sample)": (date(2010, 1, 1), date(2019, 12, 31)),
    "2020-now (out-of-sample)": (date(2020, 1, 1), date.max),
}
START_EQUITY = 1000.0


def _in(trades: list[Trade], lo: date, hi: date) -> list[Trade]:
    return [t for t in trades if lo <= t.entry_date <= hi]


def _row(label: str, s: dict) -> str:
    if not s["n"]:
        return f"  {label:28} {'no trades':>6}"
    return (f"  {label:28} {s['n']:6} {s['win_rate']:6.0%} {s['avg_ret']:+8.1%} "
            f"{s['profit_factor']:6.2f} {s['affordable']:7.0%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=50.0, help="max $ risk per trade")
    ap.add_argument("--no-stop", action="store_true", help="hold to take-profit or time exit")
    ap.add_argument("--iv-markup", type=float, default=1.1)
    ap.add_argument("--slippage", type=float, default=0.05, help="$ per spread per side")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    p = SpreadParams(risk_budget=args.budget, iv_markup=args.iv_markup, slippage=args.slippage,
                     stop_loss=None if args.no_stop else 0.5)
    frames = {t: prepare(load_bars(t)) for t in ETFS + STOCKS}

    trades: dict[str, list[Trade]] = {}
    for name, fn in STRATEGIES.items():
        trades[name] = [tr for t, df in frames.items() for tr in run_strategy(df, t, name, fn(df), p)]
    trades["baseline_bull"] = [tr for t, df in frames.items()
                               for tr in run_strategy(df, t, "baseline_bull", every_day(df), p)]
    trades["baseline_bear"] = [tr for t, df in frames.items()
                               for tr in run_strategy(df, t, "baseline_bear", -every_day(df), p)]

    print(f"\nParams: {p}\n")
    header = f"  {'':28} {'trades':>6} {'win':>6} {'avg ret':>8} {'PF':>6} {'afford':>7}"
    for period, (lo, hi) in PERIODS.items():
        print(f"=== {period} — per trade, return on debit ===")
        print(header)
        for name, ts in trades.items():
            pt = _in(ts, lo, hi)
            print(_row(name, summarize(pt)))
            for side in ("bull", "bear"):
                if not name.startswith("baseline"):
                    print(_row(f"  {side}", summarize([t for t in pt if t.direction == side])))
            if not name.startswith("baseline"):
                print(_row("  ETFs only", summarize([t for t in pt if t.ticker in ETFS])))
        print()

    print(f"=== ${START_EQUITY:,.0f} account, ${p.risk_budget:.0f} risk/trade, max 4 open ===")
    spy = frames["SPY"]["close"]
    for period, (lo, hi) in PERIODS.items():
        s = spy[(spy.index >= pd.Timestamp(lo)) & (spy.index <= pd.Timestamp(min(hi, date.today())))]
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        print(f"  {period}: SPY buy-and-hold CAGR {(s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1:+.1%}")
        for name in STRATEGIES:
            r = simulate_portfolio(_in(trades[name], lo, hi), START_EQUITY, p)
            print(f"    {name:20} ${r.start_equity:,.0f} → ${r.end_equity:,.0f}  CAGR {r.cagr:+6.1%}  "
                  f"maxDD {r.max_drawdown:5.1%}  taken {r.trades_taken}  skipped {r.trades_skipped}")


if __name__ == "__main__":
    main()
