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
    status: Literal["submitted", "dry_run", "error"]
    reason: str = ""
    executed_at: datetime
