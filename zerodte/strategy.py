"""
Pure decision logic for the 0DTE runner — the rule validated by backtest/zerodte.py
(opening-range breakout, $0.80–1.10 premium, take profit 2x, out by 15:30 ET).

No I/O here so every branch is unit-testable; zerodte/main.py does the API calls.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

OR_START = time(9, 30)
OR_END = time(9, 45)        # opening range = bars starting 9:30–9:44
DECIDE = time(10, 0)        # signal price = close of the 9:59 bar
ENTRY_END = time(10, 5)     # backtest buys on the first 10:00–10:04 bar; the buy order works until here
MIN_FILL_SECS = 60
STRIKE_STEPS = 12           # $1 SPY strikes scanned beyond the money


@dataclass
class Bar:
    start: datetime         # ET
    high: float
    low: float
    close: float


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    ts: datetime | None = None  # quote time; the free "indicative" feed may lag

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


def occ(expiry: date, side: str, strike: int) -> str:
    return f"SPY{expiry:%y%m%d}{side}{strike * 1000:08d}"


def breakout_signal(bars: list[Bar]) -> tuple[str | None, float | None, str]:
    """Return (side "C"/"P"/None, signal price, human-readable reason)."""
    rng = [b for b in bars if OR_START <= b.start.time() < OR_END]
    pre = [b for b in bars if b.start.time() < DECIDE]
    # IEX bars can skip quiet minutes; tolerate a few gaps but not a stale price
    if len(rng) < 10 or not pre or pre[-1].start.time() < time(9, 55):
        last = f"{pre[-1].start:%H:%M}" if pre else "none"
        return None, None, f"incomplete bars ({len(rng)} in opening range, last bar {last})"
    hi, lo, price = max(b.high for b in rng), min(b.low for b in rng), pre[-1].close
    if price > hi:
        return "C", price, f"SPY {price:.2f} broke above opening-range high {hi:.2f}"
    if price < lo:
        return "P", price, f"SPY {price:.2f} broke below opening-range low {lo:.2f}"
    return None, price, f"SPY {price:.2f} inside opening range {lo:.2f}–{hi:.2f}"


def candidate_symbols(expiry: date, side: str, price: float) -> list[str]:
    """Out-of-the-money same-day contracts on the breakout side."""
    if side == "C":
        first = math.floor(price) + 1
        return [occ(expiry, "C", first + i) for i in range(STRIKE_STEPS)]
    first = math.ceil(price) - 1
    return [occ(expiry, "P", first - i) for i in range(STRIKE_STEPS)]


def pick_contract(quotes: list[Quote], premium_min: float, premium_max: float,
                  max_spread: float) -> Quote | None:
    """Contract whose mid is inside the band and closest to its centre, with a sane spread."""
    target = (premium_min + premium_max) / 2
    ok = [q for q in quotes
          if q.bid > 0 and q.ask > 0 and q.ask - q.bid <= max_spread
          and premium_min <= q.mid <= premium_max]
    return min(ok, key=lambda q: abs(q.mid - target)) if ok else None


def describe_quotes(quotes: list[Quote], premium_min: float, premium_max: float,
                    max_spread: float, now: datetime) -> str:
    """Why pick_contract found nothing — which filter removed the contracts."""
    if not quotes:
        return "no quotes returned"
    live = [q for q in quotes if q.bid > 0 and q.ask > 0]
    in_band = [q for q in live if premium_min <= q.mid <= premium_max]
    parts = [f"{len(quotes)} quotes, {len(live)} two-sided, {len(in_band)} with mid in band"]
    if in_band:
        parts.append(f"narrowest in-band spread ${min(q.ask - q.bid for q in in_band):.2f} "
                     f"(limit ${max_spread:.2f})")
    elif live:
        nearest = min(live, key=lambda q: abs(q.mid - (premium_min + premium_max) / 2))
        parts.append(f"nearest mid ${nearest.mid:.2f} ({nearest.symbol})")
    ages = [(now - q.ts).total_seconds() for q in quotes if q.ts]
    if ages:
        parts.append(f"quote age {min(ages):.0f}–{max(ages):.0f}s")
    return "; ".join(parts)


def quote_log(quotes: list[Quote]) -> list[str]:
    """Compact, audit-friendly record of every candidate quote."""
    return [f"{q.symbol} {q.bid:.2f}/{q.ask:.2f}" + (f" @{q.ts:%H:%M:%S}" if q.ts else "")
            for q in quotes]


def fill_wait_secs(now: datetime) -> float:
    """How long the buy order may work: until ENTRY_END, but never less than MIN_FILL_SECS."""
    end = datetime.combine(now.date(), ENTRY_END, now.tzinfo)
    return max((end - now).total_seconds(), MIN_FILL_SECS)


def contracts_to_buy(equity: float, buying_power: float, ask: float, risk_fraction: float) -> int:
    """risk_fraction × current equity, at least 1 contract if buying power allows."""
    cost = ask * 100
    n = max(int(risk_fraction * equity // cost), 1)
    return min(n, int(buying_power // cost))


def take_profit_price(fill: float, multiple: float) -> float:
    return round(fill * multiple, 2)
