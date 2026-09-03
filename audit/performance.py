"""
Reconstructs realized trade outcomes from the executions audit trail and rolls
them up into per-ticker stats, so the gate can act on the account's own track
record — without ever showing raw P&L to the LLMs (see CLAUDE.md core rules).

Models stay blind. This is the only layer that "remembers."
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from audit.reader import load_stage

log = logging.getLogger("audit.performance")

_LOOKBACK_DAYS = 90

# Every closed trade before this date was forced shut by one of two bugs fixed
# in 5294b20 (weak-HOLD auto-close firing regardless of P&L; a no-consensus vote
# — infra noise, e.g. a rate-limited model call — treated as a reason to exit).
# Confirmed via the audit trail: 100% of closes before this date trace back to
# one of those two, none were a genuine 2/3-model consensus CLOSE. Track record
# from before this date isn't a real performance signal, so it's excluded here
# rather than left to quietly keep tickers locked out on contaminated history.
_TRACK_RECORD_SINCE = date(2026, 8, 30)


@dataclass
class ClosedTrade:
    ticker: str
    occ_symbol: str
    open_price: float
    close_price: float
    return_pct: float
    win: bool
    closed_at: str  # ISO timestamp of the CLOSE execution


@dataclass
class TickerStats:
    ticker: str
    trades: list[ClosedTrade] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float | None:
        return sum(t.win for t in self.trades) / self.n if self.n else None

    @property
    def avg_return_pct(self) -> float | None:
        return sum(t.return_pct for t in self.trades) / self.n if self.n else None


def _reconcile(executions: list[dict]) -> list[ClosedTrade]:
    """Pair each opening execution with the next CLOSE on the same occ_symbol.

    `executions` must already be in chronological order (audit.reader.load_stage
    sorts by executed_at) so the first CLOSE seen after an open is its exit.
    """
    open_by_symbol: dict[str, dict] = {}
    closed: list[ClosedTrade] = []

    # "submitted" is the legacy status for an accepted order; "filled" is written
    # once the executor confirms the fill. Both are real orders — everything else
    # (error, skipped, dry_run) never reached the market.
    for ex in executions:
        if ex.get("status") not in ("submitted", "filled"):
            continue
        occ = ex.get("occ_symbol")
        if not occ:
            continue

        if ex.get("action") in ("BUY_CALL", "BUY_PUT"):
            if ex.get("filled_avg_price") is not None:
                open_by_symbol[occ] = ex
        elif ex.get("action") == "CLOSE":
            open_ex = open_by_symbol.pop(occ, None)
            if open_ex is None or ex.get("filled_avg_price") is None:
                continue
            open_price = float(open_ex["filled_avg_price"])
            close_price = float(ex["filled_avg_price"])
            return_pct = (close_price - open_price) / open_price
            closed.append(ClosedTrade(
                ticker=ex["ticker"],
                occ_symbol=occ,
                open_price=open_price,
                close_price=close_price,
                return_pct=return_pct,
                win=return_pct > 0,
                closed_at=ex.get("executed_at") or ex.get("run_at") or "",
            ))
    return closed


def get_ticker_stats(lookback_days: int = _LOOKBACK_DAYS) -> dict[str, TickerStats]:
    """Load recent execution history and roll it up per ticker.

    Returns {} if audit S3 is disabled or has no history yet — callers must treat
    that as "no track record", not an error.
    """
    since = max(date.today() - timedelta(days=lookback_days), _TRACK_RECORD_SINCE)
    executions = load_stage("executions", since)
    if not executions:
        return {}

    stats: dict[str, TickerStats] = {}
    for trade in _reconcile(executions):
        stats.setdefault(trade.ticker, TickerStats(ticker=trade.ticker)).trades.append(trade)
    return stats
