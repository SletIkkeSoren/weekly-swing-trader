"""
SPY 0DTE option buying on real traded prices, plus a $1,000 bankroll simulation.

Unlike the other backtests there is no pricing model: every option price is an
actual 1-minute bar of the real contract, from Alpaca (history starts Feb 2024).

Two steps:

    python -m backtest.zerodte fetch     # needs ALPACA_API_KEY / ALPACA_SECRET_KEY
    python -m backtest.zerodte run       # offline, reads the cache

Rules (fixed up front, not tuned):
  - opening range = 9:30–9:44 ET; decide at 10:00 ET on the 9:59 close
  - direction: "orb" = calls above the range high, puts below the low, else no trade;
    "trend" = direction of 9:30 open → 9:59 close; "calls"/"puts"/"random" are baselines
  - contract: the out-of-the-money same-day contract whose 9:59 price is closest
    to the target premium
  - buy at the first 10:00–10:04 bar's open + slippage
  - exit: limit at entry × take-profit if a later bar trades above it, else sell
    at the last bar ≤ 15:30 ET − slippage (brokers restrict expiring contracts late)
2024 is in-sample, 2025→ out-of-sample.
"""

import argparse
import logging
import os
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backtest.data import load_bars

log = logging.getLogger("backtest.zerodte")

ET = ZoneInfo("America/New_York")
_CACHE = Path(os.getenv("BACKTEST_CACHE", ".cache/backtest")) / "zerodte"
FIRST_DAY = date(2024, 2, 1)
STRIKE_OFFSETS = range(-12, 13)     # $1 strikes around the 10:00 price

OR_END = time(9, 45)
DECIDE = time(10, 0)
ENTRY_WINDOW = time(10, 5)
CUTOFF = time(15, 30)

PERIODS = {
    "2024 (in-sample)": (date(2024, 1, 1), date(2024, 12, 31)),
    "2025-now (out-of-sample)": (date(2025, 1, 1), date(2100, 1, 1)),
}


def occ(expiry: date, kind: str, strike: int) -> str:
    return f"SPY{expiry:%y%m%d}{kind}{strike * 1000:08d}"


# ── fetch ──────────────────────────────────────────────────────────────────


def _trading_days() -> list[date]:
    daily = load_bars("SPY", "1993-01-01", adjusted=False, refresh=True)
    return [d.date() for d in daily.index if FIRST_DAY <= d.date() < date.today()]


def fetch() -> None:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionBarsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    stocks = StockHistoricalDataClient(key, secret)
    options = OptionHistoricalDataClient(key, secret)
    _CACHE.mkdir(parents=True, exist_ok=True)
    feed = DataFeed(os.getenv("ALPACA_DATA_FEED", "sip"))

    days = _trading_days()
    todo = [d for d in days if not (_CACHE / f"{d}.csv").exists()]
    print(f"{len(days)} trading days since {FIRST_DAY}, {len(todo)} to fetch")

    for n, day in enumerate(todo, 1):
        start = datetime.combine(day, time(9, 30), ET)
        end = datetime.combine(day, time(16, 0), ET)
        try:
            spy = stocks.get_stock_bars(StockBarsRequest(
                symbol_or_symbols="SPY", timeframe=TimeFrame.Minute, start=start, end=end, feed=feed)).df
        except Exception as exc:
            if feed != DataFeed.IEX:
                print(f"  {feed.value} stock feed refused ({exc}); falling back to iex")
                feed = DataFeed.IEX
                spy = stocks.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols="SPY", timeframe=TimeFrame.Minute, start=start, end=end, feed=feed)).df
            else:
                raise
        if spy.empty:
            print(f"  {day}: no SPY bars, skipped")
            continue
        spy = spy.reset_index()
        ts = pd.to_datetime(spy["timestamp"]).dt.tz_convert(ET)
        ref = spy.loc[ts.dt.time < DECIDE, "close"]
        center = round(float(ref.iloc[-1] if len(ref) else spy["close"].iloc[0]))

        symbols = [occ(day, k, center + o) for k in "CP" for o in STRIKE_OFFSETS]
        opt = options.get_option_bars(OptionBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Minute, start=start, end=end)).df
        opt = opt.reset_index() if not opt.empty else pd.DataFrame(columns=spy.columns)

        cols = ["symbol", "timestamp", "open", "high", "low", "close", "volume"]
        pd.concat([spy[cols], opt[cols]]).to_csv(_CACHE / f"{day}.csv", index=False)
        if n % 25 == 0 or n == len(todo):
            print(f"  {n}/{len(todo)}  {day}  ({len(opt)} option bars)")


# ── simulate ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    direction: str          # orb | trend | calls | puts | random
    premium: float          # target entry price per share
    take_profit: float | None  # multiple of entry; None = hold to cutoff
    band: tuple[float, float] | None = None  # only contracts priced inside this range

    def __str__(self) -> str:
        tp = f"{self.take_profit:g}x" if self.take_profit else "hold"
        prem = f"${self.band[0]:.2f}-{self.band[1]:.2f}" if self.band else f"${self.premium:.2f}"
        return f"{self.direction:6} {prem} tp {tp:4}"


@dataclass
class Trade:
    day: date
    symbol: str
    cost: float             # per share, incl. slippage
    exit: float
    hit_tp: bool

    @property
    def ret(self) -> float:
        return self.exit / self.cost - 1


def load_day(path: Path) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
    df["t"] = df["timestamp"].dt.time
    groups = {s: g.sort_values("timestamp").reset_index(drop=True) for s, g in df.groupby("symbol")}
    return groups.pop("SPY", pd.DataFrame()), groups


def _direction(spy: pd.DataFrame, mode: str, rng: random.Random) -> str | None:
    pre = spy[spy["t"] < DECIDE]
    if pre.empty:
        return None
    price = pre["close"].iloc[-1]
    if mode == "orb":
        rng_bars = spy[spy["t"] < OR_END]
        if price > rng_bars["high"].max():
            return "C"
        if price < rng_bars["low"].min():
            return "P"
        return None
    if mode == "trend":
        return "C" if price > pre["open"].iloc[0] else "P"
    if mode == "random":
        return rng.choice("CP")
    return {"calls": "C", "puts": "P"}[mode]


def simulate_day(day: date, spy: pd.DataFrame, opts: dict[str, pd.DataFrame], rule: Rule,
                 slippage: float, rng: random.Random) -> Trade | None:
    side = _direction(spy, rule.direction, rng)
    if side is None:
        return None
    price = spy[spy["t"] < DECIDE]["close"].iloc[-1]

    # OTM contracts of the chosen side, priced at their last trade before 10:00
    candidates = []
    for sym, bars in opts.items():
        if sym[9] != side:
            continue
        strike = int(sym[10:]) / 1000
        if (side == "C" and strike <= price) or (side == "P" and strike >= price):
            continue
        pre = bars[bars["t"] < DECIDE]
        if pre.empty or pre["close"].iloc[-1] < 0.02:
            continue
        last = pre["close"].iloc[-1]
        if rule.band and not rule.band[0] <= last <= rule.band[1]:
            continue
        candidates.append((abs(last - rule.premium), sym))
    if not candidates:
        return None
    sym = min(candidates)[1]
    bars = opts[sym]

    entry_bars = bars[(bars["t"] >= DECIDE) & (bars["t"] < ENTRY_WINDOW)]
    if entry_bars.empty:
        return None
    cost = entry_bars["open"].iloc[0] + slippage
    after = bars[(bars["timestamp"] > entry_bars["timestamp"].iloc[0]) & (bars["t"] <= CUTOFF)]

    if rule.take_profit:
        limit = cost * rule.take_profit
        hit = after[after["high"] > limit]
        if not hit.empty:
            return Trade(day, sym, cost, limit, True)
    last = after["close"].iloc[-1] if not after.empty else entry_bars["close"].iloc[-1]
    return Trade(day, sym, cost, max(last - slippage, 0.0), False)


def run_rule(days: list[tuple[date, pd.DataFrame, dict]], rule: Rule, slippage: float,
             seed: int = 0) -> list[Trade]:
    rng = random.Random(seed)
    return [t for d, spy, opts in days if (t := simulate_day(d, spy, opts, rule, slippage, rng))]


# ── bankroll ───────────────────────────────────────────────────────────────


@dataclass
class Bankroll:
    p_target: float
    p_ruin: float
    p_up: float
    median_end: float


def bankroll(trades: list[Trade], fraction: float, n_trades: int, start: float = 1000.0,
             target: float = 7000.0, paths: int = 10_000, seed: int = 1) -> Bankroll:
    """Bootstrap trade sequences; size = fraction × current equity, min 1 contract if affordable.

    A path stops when it reaches `target` (counted as success) or can no longer
    afford the cheapest contract in the sample (ruin).
    """
    rng = np.random.default_rng(seed)
    costs = np.array([t.cost for t in trades]) * 100
    exits = np.array([t.exit for t in trades]) * 100
    floor = costs.min()
    hit = ruin = up = 0
    ends = []
    for _ in range(paths):
        eq = start
        for i in rng.integers(0, len(trades), n_trades):
            if eq < floor:
                break
            if eq < costs[i]:
                continue
            n = max(int(fraction * eq // costs[i]), 1)
            eq += n * (exits[i] - costs[i])
            if eq >= target:
                break
        hit += eq >= target
        ruin += eq < floor
        up += eq > start
        ends.append(eq)
    return Bankroll(hit / paths, ruin / paths, up / paths, float(np.median(ends)))


def summarize(trades: list[Trade]) -> str:
    if not trades:
        return "no trades"
    r = np.array([t.ret for t in trades])
    return (f"n {len(r):3}  win {np.mean(r > 0):4.0%}  tp-hit {np.mean([t.hit_tp for t in trades]):4.0%}  "
            f"avg {r.mean():+6.1%}  median {np.median(r):+6.1%}  best {r.max():+6.0%}")


def run(slippage: float, fractions: list[float], band: tuple[float, float] | None = None) -> None:
    files = sorted(_CACHE.glob("*.csv"))
    if not files:
        raise SystemExit("No cached data — run `python -m backtest.zerodte fetch` first")
    days = []
    for f in files:
        spy, opts = load_day(f)
        if not spy.empty and opts:
            days.append((date.fromisoformat(f.stem), spy, opts))
    print(f"{len(days)} days loaded ({days[0][0]} → {days[-1][0]}), slippage ${slippage:.2f}/side\n")

    directions = ("orb", "trend", "calls", "puts", "random")
    if band:
        rules = [Rule(d, sum(band) / 2, tp, band) for d in directions for tp in (1.5, 2.0, 3.0, 5.0, None)]
    else:
        rules = [Rule(d, p, tp) for d in directions
                 for p in (0.10, 0.25, 0.50, 1.00) for tp in (2.0, 3.0, 5.0, None)]
    results = {}
    for period, (lo, hi) in PERIODS.items():
        sub = [x for x in days if lo <= x[0] <= hi]
        print(f"=== {period}: {len(sub)} days — return per trade on premium ===")
        for rule in rules:
            trades = run_rule(sub, rule, slippage)
            results[(period, rule)] = trades
            print(f"  {rule}  {summarize(trades)}")
        print()

    # Pick the best rule in-sample by average return, then judge it out-of-sample
    ins, oos = PERIODS
    best = max(rules, key=lambda r: np.mean([t.ret for t in results[(ins, r)]] or [-9]))
    print(f"Best in-sample rule: {best}")
    print(f"  in-sample:     {summarize(results[(ins, best)])}")
    print(f"  out-of-sample: {summarize(results[(oos, best)])}\n")

    oos_trades = results[(oos, best)]
    if not oos_trades:
        return
    print(f"=== $1,000 → $7,000 in ~5 months, bootstrapped from out-of-sample trades of the best rule ===")
    print("  (PDT: ≤3 same-day round trips per 5 days in a margin account under $25k ≈ 63 trades;"
          " no PDT ≈ 105)")
    for label, n in (("PDT, 63 trades", 63), ("no PDT, 105 trades", 105)):
        for f in fractions:
            b = bankroll(oos_trades, f, n)
            print(f"  {label:18} risk {f:4.0%}/trade   P(hit $7k) {b.p_target:5.1%}   "
                  f"P(ruin) {b.p_ruin:5.1%}   P(up at all) {b.p_up:5.1%}   median end ${b.median_end:,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "run"])
    ap.add_argument("--slippage", type=float, default=0.01, help="$ per share per side (≈ half the bid/ask)")
    ap.add_argument("--fractions", default="0.05,0.1,0.2,0.33,0.5")
    ap.add_argument("--band", help="only contracts priced in this range at 10:00, e.g. 0.80,1.10")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if a.cmd == "fetch":
        fetch()
    else:
        band = tuple(float(x) for x in a.band.split(",")) if a.band else None
        run(a.slippage, [float(x) for x in a.fractions.split(",")], band)


if __name__ == "__main__":
    main()
