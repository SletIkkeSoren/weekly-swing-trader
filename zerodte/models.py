from datetime import date
from typing import Literal

from pydantic import BaseModel

Status = Literal["skipped", "unfilled", "open", "take_profit", "cutoff", "error", "dry_run"]


class ZeroDteTrade(BaseModel):
    """One day of the 0DTE runner, from entry decision to exit. Persisted as state
    between the enter and exit jobs, and appended to the audit trail."""
    day: date
    status: Status
    reason: str = ""
    side: str | None = None             # "C" / "P"
    symbol: str | None = None
    qty: int = 0
    entry_price: float | None = None    # avg fill per share
    tp_price: float | None = None
    tp_order_id: str | None = None
    exit_price: float | None = None     # avg across TP fill and cutoff sale
    equity_before: float | None = None
    limit_price: float | None = None    # buy limit sent (top of the premium band)
    quotes: list[str] = []              # every candidate "symbol bid/ask @time" seen at entry

    @property
    def pnl(self) -> float | None:
        if self.entry_price is None or self.exit_price is None:
            return None
        return (self.exit_price - self.entry_price) * 100 * self.qty
