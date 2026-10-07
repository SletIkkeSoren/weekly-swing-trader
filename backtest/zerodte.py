"""
0DTE option buying (SPY, QQQ, IWM, …) on real traded prices, plus a $1,000 bankroll simulation.

Unlike the other backtests there is no pricing model: every option price is an
actual 1-minute bar of the real contract, from Alpaca (history starts Feb 2024).

Two steps:

    python -m backtest.zerodte fetch [--ticker QQQ]   # needs ALPACA_API_KEY / ALPACA_SECRET_KEY
    python -m backtest.zerodte run   [--ticker QQQ]   # offline, reads the cache
    python -m backtest.zerodte compare --tickers SPY,QQQ,IWM   # live rule, out-of-sample, side by side

Cache: SPY keeps the original `.cache/backtest/zerodte/*.csv`; every other ticker
gets `.cache/backtest/zerodte/<TICKER>/`. A cached day with stock bars but no
option bars had no same-day expiry (or Alpaca has none) — it is skipped and
reported as uncovered, never counted as "no signal".

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


@dataclass(frozen=True)
class StrikeGrid:
    """Which strikes `fetch` asks for around the 9:59 price. Data coverage only, not a rule."""
    step: float
    n: int                  # strikes on each side of the center

    def strikes(self, price: float) -> list[float]:
        center = round(price / self.step) * self.step
        return [round(center + i * self.step, 3) for i in range(-self.n, self.n + 1)]


# SPY/QQQ list $1 strikes near the money on same-day expiries. IWM (~$200) is asked
# at $0.50: if those strikes don't exist Alpaca just returns no bars for them, and
# `run` reports the spacing it actually found.
STRIKE_GRIDS = {
    "SPY": StrikeGrid(1.0, 12),
    "QQQ": StrikeGrid(1.0, 15),
    "IWM": StrikeGrid(0.5, 16),
}
DEFAULT_GRID = StrikeGrid(1.0, 12)

OR_END = time(9, 45)
DECIDE = time(10, 0)
ENTRY_WINDOW = time(10, 5)
CUTOFF = time(15, 30)

PERIODS = {
    "2024 (in-sample)": (date(2024, 1, 1), date(2024, 12, 31)),
    "2025-now (out-of-sample)": (date(2025, 1, 1), date(2100, 1, 1)),
}


def occ(expiry: date, kind: str, strike: float, ticker: str = "SPY") -> str:
    return f"{ticker}{expiry:%y%m%d}{kind}{round(strike * 1000):08d}"


def parse_occ(symbol: str) -> tuple[str, str, float]:
    """(root, C|P, strike) — parsed from the right, so any root length works."""
    return symbol[:-15], symbol[-9], int(symbol[-8:]) / 1000


def cache_dir(ticker: str) -> Path:
    """SPY keeps the pre-multi-ticker layout so its existing cache stays valid."""
    ticker = ticker.upper()
    return _CACHE if ticker == "SPY" else _CACHE / ticker


# ── fetch ──────────────────────────────────────────────────────────────────


def _trading_days(ticker: str) -> list[date]:
    daily = load_bars(ticker, "1993-01-01", adjusted=False, refresh=True)
    return [d.date() for d in daily.index if FIRST_DAY <= d.date() < date.today()]


def fetch(ticker: str = "SPY") -> None:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionBarsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    stocks = StockHistoricalDataClient(key, secret)
    options = OptionHistoricalDataClient(key, secret)
    ticker = ticker.upper()
    cache = cache_dir(ticker)
    cache.mkdir(parents=True, exist_ok=True)
    grid = STRIKE_GRIDS.get(ticker, DEFAULT_GRID)
    feed = DataFeed(os.getenv("ALPACA_DATA_FEED", "sip"))

    days = _trading_days(ticker)
    todo = [d for d in days if not (cache / f"{d}.csv").exists()]
    print(f"{ticker}: {len(days)} trading days since {FIRST_DAY}, {len(todo)} to fetch → {cache}")

    failures = uncovered = 0
    for n, day in enumerate(todo, 1):
        start = datetime.combine(day, time(9, 30), ET)
        end = datetime.combine(day, time(16, 0), ET)
        try:
            stock = stocks.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=ticker, timeframe=TimeFrame.Minute, start=start, end=end, feed=feed)).df
        except Exception as exc:
            if feed != DataFeed.IEX:
                print(f"  {feed.value} stock feed refused ({exc}); falling back to iex")
                feed = DataFeed.IEX
                stock = stocks.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=ticker, timeframe=TimeFrame.Minute, start=start, end=end, feed=feed)).df
            else:
                raise
        if stock.empty:
            print(f"  {day}: no {ticker} bars, skipped")
            continue
        stock = stock.reset_index()
        ts = pd.to_datetime(stock["timestamp"]).dt.tz_convert(ET)
        ref = stock.loc[ts.dt.time < DECIDE, "close"]
        price = float(ref.iloc[-1] if len(ref) else stock["close"].iloc[0])

        symbols = [occ(day, k, s, ticker) for k in "CP" for s in grid.strikes(price)]
        try:
            opt = options.get_option_bars(OptionBarsRequest(
                symbol_or_symbols=symbols, timeframe=TimeFrame.Minute, start=start, end=end)).df
        except Exception as exc:
            # not cached, so the next fetch retries it; an empty result IS cached (no expiry)
            print(f"  {day}: option bars request failed ({exc}), not cached")
            failures += 1
            if failures >= 5:
                raise SystemExit("5 option requests failed in a row — stopping")
            continue
        failures = 0

        # no option bars = no same-day expiry that day (e.g. IWM Tue/Thu before daily
        # expiries); the stock-only file marks the day as uncovered for `run`
        cols = ["symbol", "timestamp", "open", "high", "low", "close", "volume"]
        out = stock[cols] if opt.empty else pd.concat([stock[cols], opt.reset_index()[cols]])
        out.to_csv(cache / f"{day}.csv", index=False)
        uncovered += opt.empty
        if n % 25 == 0 or n == len(todo):
            print(f"  {n}/{len(todo)}  {day}  ({len(opt)} option bars; "
                  f"{uncovered} days without same-day options so far)")


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


def load_day(path: Path, ticker: str = "SPY") -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
    df["t"] = df["timestamp"].dt.time
    groups = {s: g.sort_values("timestamp").reset_index(drop=True) for s, g in df.groupby("symbol")}
    return groups.pop(ticker.upper(), pd.DataFrame()), groups


def _direction(stock: pd.DataFrame, mode: str, rng: random.Random) -> str | None:
    pre = stock[stock["t"] < DECIDE]
    if pre.empty:
        return None
    price = pre["close"].iloc[-1]
    if mode == "orb":
        rng_bars = stock[stock["t"] < OR_END]
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


def simulate_day(day: date, stock: pd.DataFrame, opts: dict[str, pd.DataFrame], rule: Rule,
                 slippage: float, rng: random.Random) -> Trade | None:
    side = _direction(stock, rule.direction, rng)
    if side is None:
        return None
    price = stock[stock["t"] < DECIDE]["close"].iloc[-1]

    # OTM contracts of the chosen side, priced at their last trade before 10:00
    candidates = []
    for sym, bars in opts.items():
        _, kind, strike = parse_occ(sym)
        if kind != side:
            continue
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
    return [t for d, stock, opts in days if (t := simulate_day(d, stock, opts, rule, slippage, rng))]


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


Day = tuple[date, pd.DataFrame, dict[str, pd.DataFrame]]


@dataclass
class Coverage:
    ticker: str
    days: list[Day]         # stock bars and same-day option bars
    uncovered: list[date]   # stock bars, no same-day option bars → skipped

    def report(self) -> str:
        files = sorted([d for d, _, _ in self.days] + self.uncovered)
        if not files:
            return f"{self.ticker}: empty cache"
        lines = [f"{self.ticker}: {len(files)} cached days {files[0]} → {files[-1]}, "
                 f"{len(self.days)} with same-day options, {len(self.uncovered)} without (skipped)"]
        if self.days:
            first = self.days[0][0]
            late = [d for d in self.uncovered if d > first]
            lines.append(f"  first covered day {first}; uncovered after it: {len(late)}"
                         + (f" (by weekday {_weekdays(late)})" if late else ""))
            if spacing := _strike_spacing(self.days):
                lines.append(f"  near-the-money strike spacing seen: {spacing}")
        return "\n".join(lines)


def _weekdays(days: list[date]) -> str:
    counts = pd.Series([d.strftime("%a") for d in days]).value_counts()
    return " ".join(f"{k}:{v}" for k, v in counts.items())


def _strike_spacing(days: list[Day]) -> str:
    """Smallest gap between strikes that traded, per day → how often each gap occurs."""
    gaps = []
    for _, _, opts in days:
        strikes = sorted({parse_occ(s)[2] for s in opts})
        if len(strikes) > 1:
            gaps.append(min(np.diff(strikes)))
    if not gaps:
        return ""
    counts = pd.Series(gaps).round(2).value_counts()
    return ", ".join(f"${g:g} on {n} days" for g, n in counts.items())


def load_days(ticker: str) -> Coverage:
    ticker = ticker.upper()
    files = sorted(cache_dir(ticker).glob("*.csv"))
    if not files:
        raise SystemExit(f"No cached data for {ticker} — run "
                         f"`python -m backtest.zerodte fetch --ticker {ticker}` first")
    days, uncovered = [], []
    for f in files:
        stock, opts = load_day(f, ticker)
        if stock.empty:
            continue
        if opts:
            days.append((date.fromisoformat(f.stem), stock, opts))
        else:
            uncovered.append(date.fromisoformat(f.stem))
    return Coverage(ticker, days, uncovered)


def run(slippage: float, fractions: list[float], band: tuple[float, float] | None = None,
        ticker: str = "SPY") -> None:
    cov = load_days(ticker)
    days = cov.days
    if not days:
        raise SystemExit(cov.report())
    print(cov.report())
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


# ── compare tickers ────────────────────────────────────────────────────────


@dataclass
class Equity:
    final: float
    max_dd: float           # worst peak-to-trough, fraction of the peak
    skipped: int            # trades the bankroll could not afford


def equity_path(trades: list[Trade], fraction: float, start: float = 1000.0) -> Equity:
    """Trades in date order, sized like the live runner: fraction × current equity, ≥ 1 contract."""
    eq = peak = start
    max_dd, skipped = 0.0, 0
    for t in sorted(trades, key=lambda t: t.day):
        cost = t.cost * 100
        if eq < cost:
            skipped += 1
            continue
        eq += max(int(fraction * eq // cost), 1) * (t.exit - t.cost) * 100
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    return Equity(eq, max_dd, skipped)


def _stats(trades: list[Trade], fraction: float) -> dict[str, float]:
    r = np.array([t.ret for t in trades]) if trades else np.array([np.nan])
    eq = equity_path(trades, fraction)
    return {"trades": len(trades), "win": float(np.mean(r > 0)) if trades else np.nan,
            "avg": float(np.mean(r)), "pnl": eq.final - 1000.0, "dd": eq.max_dd,
            "unaffordable": eq.skipped}


def random_baselines(days: list[Day], rule: Rule, slippage: float, seeds: int) -> list[list[Trade]]:
    """Random-direction runs, `seeds` of them. Each day's call and put trade is
    simulated once; a seed only decides which side it takes."""
    rng0 = random.Random(0)  # unused by calls/puts
    calls = [simulate_day(d, s, o, Rule("calls", rule.premium, rule.take_profit, rule.band), slippage, rng0)
             for d, s, o in days]
    puts = [simulate_day(d, s, o, Rule("puts", rule.premium, rule.take_profit, rule.band), slippage, rng0)
            for d, s, o in days]
    out = []
    for seed in range(seeds):
        rng = random.Random(seed)
        out.append([t for c, p in zip(calls, puts) if (t := c if rng.choice("CP") == "C" else p)])
    return out


def signal_overlap(signals: dict[str, dict[date, str | None]]) -> list[str]:
    """How often tickers break out on the same day, and in the same direction."""
    tickers = list(signals)
    common = sorted(set.intersection(*(set(s) for s in signals.values())))
    if not common:
        return ["  no days covered by all tickers"]
    lines = [f"  {len(common)} days covered by all of {', '.join(tickers)} "
             f"({common[0]} → {common[-1]})"]
    for t in tickers:
        n = sum(signals[t][d] is not None for d in common)
        lines.append(f"    {t}: signal on {n} days ({n / len(common):.0%})")
    k = len(tickers)
    for need in range(2, k + 1):
        any_dir = same = 0
        for d in common:
            sides = [signals[t][d] for t in tickers if signals[t][d]]
            if len(sides) >= need:
                any_dir += 1
                same += max(sides.count("C"), sides.count("P")) >= need
        label = f"all {k}" if need == k else f"≥{need}"
        lines.append(f"    {label} signal the same day: {any_dir} days; same direction: {same}")
    for i, a in enumerate(tickers):
        for b in tickers[i + 1:]:
            both = [d for d in common if signals[a][d] and signals[b][d]]
            agree = sum(signals[a][d] == signals[b][d] for d in both)
            either = sum(bool(signals[a][d] or signals[b][d]) for d in common)
            lines.append(f"    {a}/{b}: both signal {len(both)} of {either} days either does, "
                         f"same direction {agree}/{len(both)}")
    return lines


def compare(tickers: list[str], slippage: float, band: tuple[float, float], take_profit: float,
            fraction: float, seeds: int = 200) -> None:
    """The live rule on each ticker, out-of-sample only, against a random-direction baseline."""
    rule = Rule("orb", sum(band) / 2, take_profit, band)
    lo, hi = PERIODS["2025-now (out-of-sample)"]
    print(f"Rule: {rule}  slippage ${slippage:.2f}/side  $1,000 at {fraction:.0%} of current equity")
    print(f"Out-of-sample: {lo} onward\n")

    rows, signals, traded = [], {}, {}
    for ticker in tickers:
        try:
            cov = load_days(ticker)
        except SystemExit as exc:
            print(f"{ticker}: skipped — {exc}")
            continue
        print(cov.report())
        days = [x for x in cov.days if lo <= x[0] <= hi]
        if not days:
            continue
        rng = random.Random(0)
        signals[ticker] = {d: _direction(s, "orb", rng) for d, s, _ in days}
        trades = run_rule(days, rule, slippage)
        traded[ticker] = {t.day: parse_occ(t.symbol)[1] for t in trades}
        n_sig = sum(v is not None for v in signals[ticker].values())
        rows.append((ticker, "orb", len(days), n_sig, _stats(trades, fraction)))

        base = [_stats(tr, fraction) for tr in random_baselines(days, rule, slippage, seeds)]
        med = {k: float(np.nanmedian([b[k] for b in base])) for k in base[0]}
        beats = np.mean([b["pnl"] < rows[-1][4]["pnl"] for b in base])
        rows.append((ticker, f"random×{seeds}", len(days), None, med))
        rows[-1][4]["beats"] = beats
    print()

    print(f"{'ticker':6} {'rule':10} {'days':>5} {'signals':>7} {'trades':>6} {'win':>5} "
          f"{'avg/trade':>9} {'P&L':>9} {'max DD':>7} {'unaffd':>6}")
    for ticker, name, n_days, n_sig, st in rows:
        sig = "-" if n_sig is None else str(n_sig)
        print(f"{ticker:6} {name:10} {n_days:5} {sig:>7} {st['trades']:6.0f} {st['win']:5.0%} "
              f"{st['avg']:+9.1%} {st['pnl']:+9,.0f} {st['dd']:7.0%} {st['unaffordable']:6.0f}")
        if "beats" in st:
            print(f"{'':17}orb P&L beats {st['beats']:.0%} of the random seeds")
    print(f"\nrandom×{seeds}: median of each column over {seeds} random-direction runs "
          "(same contract pick, same exits).")
    print("signals − trades = breakouts with no contract in the band or no 10:00–10:04 trade.")
    print("unaffd = trades skipped because equity fell below one contract's cost.\n")

    if len(signals) > 1:
        print("Signal overlap (orb breakouts, out-of-sample):")
        print("\n".join(signal_overlap(signals)))
        print("Trade overlap (breakouts that actually got a contract):")
        print("\n".join(signal_overlap({t: {d: traded[t].get(d) for d in signals[t]} for t in signals})))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "run", "compare"])
    ap.add_argument("--ticker", default="SPY", help="underlying for fetch/run")
    ap.add_argument("--tickers", default="SPY,QQQ,IWM", help="underlyings for compare")
    ap.add_argument("--slippage", type=float, default=0.01, help="$ per share per side (≈ half the bid/ask)")
    ap.add_argument("--fractions", default="0.05,0.1,0.2,0.33,0.5")
    ap.add_argument("--band", help="only contracts priced in this range at 10:00, e.g. 0.80,1.10 "
                    "(compare defaults to the live 0.80,1.10)")
    ap.add_argument("--take-profit", type=float, default=2.0, help="compare: multiple of entry")
    ap.add_argument("--fraction", type=float, default=0.2, help="compare: risk per trade")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    band = tuple(float(x) for x in a.band.split(",")) if a.band else None
    if a.cmd == "fetch":
        fetch(a.ticker)
    elif a.cmd == "run":
        run(a.slippage, [float(x) for x in a.fractions.split(",")], band, a.ticker)
    else:
        compare([t.strip().upper() for t in a.tickers.split(",")], a.slippage,
                band or (0.80, 1.10), a.take_profit, a.fraction)


if __name__ == "__main__":
    main()
