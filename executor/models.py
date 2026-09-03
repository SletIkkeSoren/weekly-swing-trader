from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ExecutionResult(BaseModel):
    ticker: str
    occ_symbol: str
    action: str
    strike: float | None = None   # None for CLOSE
    expiry: str | None = None     # None for CLOSE
    contracts: int
    order_id: str | None = None
    order_type: str
    limit_price: float | None = None
    filled_avg_price: float | None = None  # None if not filled within the poll window
    # filled   — Alpaca confirmed the fill within the poll window
    # submitted— accepted by Alpaca, fill still unknown when we stopped polling
    # skipped  — deliberately not sent (e.g. no quote available)
    status: Literal["filled", "submitted", "dry_run", "skipped", "error"]
    reason: str = ""
    executed_at: datetime
