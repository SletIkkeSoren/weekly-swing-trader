"""
RSI-2 pullback traded with ETF/stock shares instead of option spreads.

Long only — the spread backtest showed the bear side loses in every config.
Same timing as the live pipeline: decisions on bar close, fills at next open.
Equity is split into `max_positions` equal slots, fractional shares, compounding.

    python -m backtest.shares [--slippage-bps 5] [--max-positions 4]
"""

import argparse
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
import ta.momentum as momentum

from backtest.data import load_bars
from backtest.main import ETFS, PERIODS, STOCKS
from backtest.strategies import rsi2_pullback

EXIT_RULES = ("sma5", "rsi70")


@dataclass
class ShareParams:
    max_positions: int = 4
    slippage_bps: float = 5.0   # per side; commission-free at Alpaca
    max_hold: int = 10          # trading days
    exit_rule: str = "sma5"     # "sma5": close > 5-day SMA, "rsi70": RSI-2 > 70
    park_in_spy: bool = False   # hold idle cash in SPY instead of 0%


@dataclass
class Result:
    cagr: float
    max_dd: float
    sharpe: float
    exposure: float             # avg fraction of equity invested
    trades: int
    win_rate: float
    avg_trade: float

    def line(self, label: str) -> str:
        extra = (f"  trades {self.trades:4}  win {self.win_rate:4.0%}  avg {self.avg_trade:+5.2%}"
                 if self.trades else "")
        return (f"  {label:34} CAGR {self.cagr:+6.1%}  maxDD {self.max_dd:5.1%}  "
                f"Sharpe {self.sharpe:5.2f}  invested {self.exposure:4.0%}{extra}")


def prepare(df: pd.DataFrame, rng: np.random.Generator | None = None) -> pd.DataFrame:
    """With `rng`, replace the RSI-2 trigger by random days at the same frequency,
    keeping the 200-day trend filter — isolates the value of the RSI-2 timing."""
    out = df.copy()
    out["signal"] = rsi2_pullback(out).clip(lower=0)
    if rng is not None:
        uptrend = out["close"] > out["close"].rolling(200).mean()
        freq = out["signal"].sum() / max(uptrend.sum(), 1)
        out["signal"] = (uptrend & (rng.random(len(out)) < freq)).astype(int)
    out["rsi2"] = momentum.rsi(out["close"], window=2)
    out["sma5"] = out["close"].rolling(5).mean()
    return out


def _metrics(equity: pd.Series, invested: pd.Series, trade_rets: list[float]) -> Result:
    daily = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    dd = (equity / equity.cummax() - 1).min()
    sharpe = daily.mean() / daily.std() * math.sqrt(252) if daily.std() > 0 else 0.0
    return Result(
        cagr=(equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1,
        max_dd=-dd,
        sharpe=sharpe,
        exposure=float(invested.mean()),
        trades=len(trade_rets),
        win_rate=sum(r > 0 for r in trade_rets) / len(trade_rets) if trade_rets else 0.0,
        avg_trade=float(np.mean(trade_rets)) if trade_rets else 0.0,
    )


def buy_and_hold(df: pd.DataFrame, lo: date, hi: date, start_equity: float = 1000.0) -> Result:
    close = df["close"][(df.index >= pd.Timestamp(lo)) & (df.index <= pd.Timestamp(hi))]
    equity = close / close.iloc[0] * start_equity
    return _metrics(equity, pd.Series(1.0, index=equity.index), [])


def simulate(frames: dict[str, pd.DataFrame], p: ShareParams, lo: date, hi: date,
             start_equity: float = 1000.0) -> Result:
    calendar = frames["SPY"].index
    calendar = calendar[(calendar >= pd.Timestamp(lo)) & (calendar <= pd.Timestamp(hi))]
    slip = p.slippage_bps / 10_000

    cash = start_equity
    held: dict[str, dict] = {}          # ticker -> {shares, cost, days}
    pending_buy: list[str] = []
    pending_sell: list[str] = []
    trade_rets: list[float] = []
    eq_curve, inv_curve = [], []
    last_equity = start_equity

    spy = frames["SPY"]
    park = 0.0                          # SPY shares held as a cash substitute

    for day in calendar:
        bars = {t: df.loc[day] for t, df in frames.items() if day in df.index}
        spy_open = spy.at[day, "open"]

        # ── Open: fills decided on yesterday's close ──────────────────────
        for t in pending_sell:
            if t not in bars:
                continue
            pos = held.pop(t)
            proceeds = pos["shares"] * bars[t]["open"] * (1 - slip)
            cash += proceeds
            trade_rets.append(proceeds / pos["cost"] - 1)
        pending_sell = [t for t in pending_sell if t in held]
        slot = last_equity / p.max_positions
        n_buys = min(len(pending_buy), p.max_positions - len(held))
        shortfall = n_buys * slot - cash
        if park and shortfall > 0:      # sell only as much parked SPY as the buys need
            sold = min(park, shortfall / (spy_open * (1 - slip)))
            park -= sold
            cash += sold * spy_open * (1 - slip)
        for t in pending_buy:
            if t not in bars or len(held) >= p.max_positions:
                continue
            spend = min(slot, cash)
            if spend < 1:
                continue
            price = bars[t]["open"] * (1 + slip)
            held[t] = {"shares": spend / price, "cost": spend, "days": 0}
            cash -= spend
        pending_buy = []
        if p.park_in_spy and cash > 1:
            park += cash / (spy_open * (1 + slip))
            cash = 0.0

        # ── Close: mark to market, then decide tomorrow's fills ───────────
        pos_value = sum(pos["shares"] * bars[t]["close"] for t, pos in held.items() if t in bars)
        last_equity = cash + pos_value + park * spy.at[day, "close"]
        eq_curve.append(last_equity)
        inv_curve.append(pos_value / last_equity if last_equity else 0.0)

        for t, pos in held.items():
            if t not in bars:
                continue
            pos["days"] += 1
            b = bars[t]
            exit_hit = b["close"] > b["sma5"] if p.exit_rule == "sma5" else b["rsi2"] > 70
            if exit_hit or pos["days"] >= p.max_hold:
                pending_sell.append(t)

        free = p.max_positions - len(held) + len(pending_sell)
        candidates = sorted(
            (b["rsi2"], t) for t, b in bars.items()
            if b["signal"] > 0 and t not in held and not np.isnan(b["rsi2"])
        )
        pending_buy = [t for _, t in candidates[:max(free, 0)]]

    idx = calendar[:len(eq_curve)]
    return _metrics(pd.Series(eq_curve, index=idx), pd.Series(inv_curve, index=idx), trade_rets)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--max-positions", type=int, default=4)
    ap.add_argument("--max-hold", type=int, default=10)
    ap.add_argument("--exit", choices=EXIT_RULES, default="rsi70")
    ap.add_argument("--seeds", type=int, default=10, help="random-entry baseline runs")
    args = ap.parse_args()

    raw = {t: load_bars(t) for t in ETFS + STOCKS}
    etfs = {t: prepare(raw[t]) for t in ETFS}
    universes = {"ETFs": etfs, "ETFs+stocks": {**etfs, **{t: prepare(raw[t]) for t in STOCKS}}}
    # Random baseline on ETFs only — the stock list is picked in hindsight
    randoms = [{t: prepare(raw[t], np.random.default_rng(s)) for t in ETFS} for s in range(args.seeds)]

    for period, (lo, hi) in PERIODS.items():
        hi = min(hi, date.today())
        print(f"\n=== {period}: $1,000 start, {args.slippage_bps:g} bps/side, "
              f"{args.max_positions} slots, max hold {args.max_hold}d, exit {args.exit} ===")
        print(buy_and_hold(raw["SPY"], lo, hi).line("SPY buy-and-hold"))
        for park in (False, True):
            p = ShareParams(max_positions=args.max_positions, slippage_bps=args.slippage_bps,
                            max_hold=args.max_hold, exit_rule=args.exit, park_in_spy=park)
            tag = " + idle in SPY" if park else ""
            real = {u: simulate(f, p, lo, hi) for u, f in universes.items()}
            for u, r in real.items():
                print(r.line(f"RSI-2 {u}{tag}"))
            rand = [simulate(f, p, lo, hi) for f in randoms]
            beat = sum(r.sharpe >= real["ETFs"].sharpe for r in rand)
            print(f"  {'random entry ETFs' + tag:34} CAGR {np.mean([r.cagr for r in rand]):+6.1%}  "
                  f"maxDD {np.mean([r.max_dd for r in rand]):5.1%}  "
                  f"Sharpe {np.mean([r.sharpe for r in rand]):5.2f}  "
                  f"(range {min(r.sharpe for r in rand):.2f}–{max(r.sharpe for r in rand):.2f}; "
                  f"{beat}/{len(rand)} ≥ RSI-2 ETFs)")


if __name__ == "__main__":
    main()
