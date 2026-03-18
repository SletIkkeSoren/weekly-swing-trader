from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from consensus.models import Action, ConsensusResult


class FilterResult(BaseModel):
    passed: bool
    reason: str  # rejection reason or "ok"


class TradeProposal(BaseModel):
    """A consensus result that cleared all risk filters, pending human approval."""
    ticker: str
    action: Action
    strike: float | None = None   # None for CLOSE (executor resolves from live position)
    expiry: date | None = None    # None for CLOSE
    confidence: float
    agreement_count: int
    suggested_contracts: int
    risk_usd: float              # account_size * risk_pct
    all_reasoning: list[str]
    all_invalidating_conditions: list[str]
    as_of: datetime

    @classmethod
    def from_consensus(
        cls,
        result: ConsensusResult,
        suggested_contracts: int,
        risk_usd: float,
    ) -> "TradeProposal":
        return cls(
            ticker=result.ticker,
            action=result.consensus_action,  # type: ignore[arg-type]
            strike=result.consensus_strike,  # type: ignore[arg-type]
            expiry=result.consensus_expiry,  # type: ignore[arg-type]
            confidence=result.consensus_confidence,  # type: ignore[arg-type]
            agreement_count=result.agreement_count,
            suggested_contracts=suggested_contracts,
            risk_usd=risk_usd,
            all_reasoning=result.all_reasoning,
            all_invalidating_conditions=result.all_invalidating_conditions,
            as_of=result.as_of,
        )


class ApprovedTrade(BaseModel):
    """A proposal that received human approval — consumed by the executor."""
    proposal: TradeProposal
    approved_at: datetime
    webhook_reason: str = ""
