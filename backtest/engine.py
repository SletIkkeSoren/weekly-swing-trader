"""
Trade simulation for vertical debit spreads.

Timing mirrors the live pipeline: signals and exit checks use bar i's close
(pre-open analysis), fills happen at bar i+1's open (in-session executor).

Sizing mirrors a small account: the spread is made as wide as the risk budget
allows, in the underlying's strike increments. A trade that can't be made
affordable at the narrowest width is flagged `affordable=False` — per-trade
stats still include it (the signal quality question), the portfolio sim skips it.
"""

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from backtest.data import historical_volatility
from backtest.pricing import Direction, spread_value

# Index/sector ETFs with $1 strikes near the money even at high prices
_DOLLAR_STRIKE_ETFS = {"SPY", "QQQ", "IWM", "DIA"}
_MIN_ADJUSTED_PRICE = 10.0


@dataclass
class SpreadParams:
    dte: int = 30               # calendar days to expiry at entry
    exit_dte: int = 7           # time exit — same as gate/filters.py _MIN_DTE
    take_profit: float = 0.6    # exit when value >= cost × (1 + take_profit)
    stop_loss: float | None = 0.5  # exit when value <= cost × (1 - stop_loss); None = hold
    risk_budget: float = 50.0   # max $ debit per trade
    slippage: float = 0.05      # $ per spread per side (both legs, mleg order)
    iv_markup: float = 1.1      # IV proxy = HV-20 × markup (implied usually > realized)
    max_width_steps: int = 20


@dataclass
class Trade:
    ticker: str
    strategy: str
    direction: Direction
    entry_date: date
    exit_date: date
    cost: float                 # debit paid per share, incl. slippage
    exit_value: float           # proceeds per share, net of slippage
    width: float
    affordable: bool
    exit_reason: str
    exit_idx: int = field(repr=False, default=0)

    @property
    def ret(self) -> float:
        """Return on the debit — 1 = doubled, -1 = total loss."""
        return (self.exit_value - self.cost) / self.cost


def strike_increment(ticker: str, price: float) -> float:
    """Approximate listed strike spacing near the money."""
    if ticker in _DOLLAR_STRIKE_ETFS:
        return 1.0
    if price < 25:
        return 0.5
    if price < 100:
        return 1.0
    if price < 250:
        return 2.5
    return 5.0


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["hv20"] = historical_volatility(out["close"])
    return out


def simulate_trade(df: pd.DataFrame, ticker: str, strategy: str, i: int,
                   direction: Direction, p: SpreadParams) -> Trade | None:
    """Enter at bar i+1's open on a signal from bar i; return the closed trade."""
    e = i + 1
    if e >= len(df) or pd.isna(df["hv20"].iat[i]):
        return None

    dates = df.index
    opens, closes, hv = df["open"].to_numpy(), df["close"].to_numpy(), df["hv20"].to_numpy()
    spot = opens[e]
    # Split-adjusted history puts pre-split NVDA/TSLA/AAPL under $1; the real
    # contracts then had real strikes at the unadjusted price, so don't invent any
    if spot < _MIN_ADJUSTED_PRICE:
        return None
    sigma = hv[i] * p.iv_markup
    inc = strike_increment(ticker, spot)
    long_k = round(spot / inc) * inc
    t0 = p.dte / 365

    # Widest spread that fits the budget; fall back to the narrowest and flag it
    width, cost, affordable = inc, None, False
    for steps in range(1, p.max_width_steps + 1):
        w = steps * inc
        c = spread_value(direction, spot, long_k, w, t0, sigma) + p.slippage
        if c * 100 > p.risk_budget:
            break
        width, cost, affordable = w, c, True
    if cost is None:
        cost = spread_value(direction, spot, long_k, width, t0, sigma) + p.slippage
    if cost < 0.01:  # degenerate pricing (e.g. zero HV on a flat bar) — no real trade here
        return None

    def value_at(idx: int, price: float, sig: float) -> float:
        remaining = p.dte - (dates[idx] - dates[e]).days
        return spread_value(direction, price, long_k, width, remaining / 365, sig)

    for j in range(e, len(df)):
        remaining = p.dte - (dates[j] - dates[e]).days
        v = value_at(j, closes[j], hv[j] * p.iv_markup)
        reason = None
        if v >= cost * (1 + p.take_profit):
            reason = "take_profit"
        elif p.stop_loss is not None and v <= cost * (1 - p.stop_loss):
            reason = "stop_loss"
        elif remaining <= p.exit_dte:
            reason = "time"
        if reason is None:
            continue
        if j + 1 < len(df):
            exit_idx, exit_v = j + 1, value_at(j + 1, opens[j + 1], hv[j] * p.iv_markup)
        else:
            exit_idx, exit_v = j, v
        return Trade(ticker, strategy, direction, dates[e].date(), dates[exit_idx].date(),
                     cost, max(exit_v - p.slippage, 0.0), width, affordable, reason, exit_idx)
    return None  # still open at end of data


def run_strategy(df: pd.DataFrame, ticker: str, strategy: str, signals: pd.Series,
                 p: SpreadParams) -> list[Trade]:
    """One position at a time per ticker/strategy; signals while in a trade are ignored."""
    trades: list[Trade] = []
    sig = signals.to_numpy()
    next_allowed = 0
    for i in range(len(df)):
        if i < next_allowed or sig[i] == 0:
            continue
        t = simulate_trade(df, ticker, strategy, i, "bull" if sig[i] > 0 else "bear", p)
        if t is None:  # unpriceable, or still open at end of data
            continue
        trades.append(t)
        next_allowed = t.exit_idx
    return trades


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t.ret for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gross_loss = -sum(losses)
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(rets),
        "avg_ret": sum(rets) / len(rets),
        "profit_factor": sum(wins) / gross_loss if gross_loss else math.inf,
        "affordable": sum(t.affordable for t in trades) / len(trades),
    }


@dataclass
class PortfolioResult:
    start_equity: float
    end_equity: float
    trades_taken: int
    trades_skipped: int
    max_drawdown: float
    years: float

    @property
    def cagr(self) -> float:
        if self.end_equity <= 0:
            return -1.0
        return (self.end_equity / self.start_equity) ** (1 / self.years) - 1


def simulate_portfolio(trades: list[Trade], start_equity: float, p: SpreadParams,
                       max_positions: int = 4) -> PortfolioResult:
    """Fixed-$ risk per trade, capped concurrent positions, one position per ticker."""
    trades = sorted(trades, key=lambda t: (t.entry_date, t.ticker))
    equity = peak = start_equity
    max_dd = 0.0
    open_pos: list[tuple[Trade, int]] = []  # (trade, contracts)
    taken = skipped = 0

    def realize_until(day: date) -> None:
        nonlocal equity, peak, max_dd
        for t, n in sorted([x for x in open_pos if x[0].exit_date <= day], key=lambda x: x[0].exit_date):
            open_pos.remove((t, n))
            equity += n * 100 * (t.exit_value - t.cost)
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)

    for t in trades:
        realize_until(t.entry_date)
        busy = any(o.ticker == t.ticker for o, _ in open_pos)
        committed = sum(n * 100 * o.cost for o, n in open_pos)
        contracts = int(p.risk_budget // (t.cost * 100)) if t.affordable else 0
        if busy or len(open_pos) >= max_positions or contracts < 1 \
                or committed + contracts * 100 * t.cost > equity:
            skipped += 1
            continue
        open_pos.append((t, contracts))
        taken += 1
    realize_until(date.max)

    years = (trades[-1].exit_date - trades[0].entry_date).days / 365.25 if trades else 1.0
    return PortfolioResult(start_equity, equity, taken, skipped, max_dd, max(years, 1e-9))
