from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from fetcher.models import OpenPosition

Action = Literal["BUY_CALL", "BUY_PUT", "CLOSE", "HOLD"]


class ModelVote(BaseModel):
    """Raw output from one model call, validated against the output schema."""
    model: str
    action: Action
    strike: float | None = None       # None when action is HOLD
    expiry: date | None = None        # Nearest/next Friday; None when action is HOLD
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: list[str]
    invalidating_conditions: list[str]


class ConsensusResult(BaseModel):
    """Aggregated result across all three models for one ticker."""
    ticker: str
    as_of: datetime
    latest_close: float
    votes: list[ModelVote]

    # Populated only when >= 2 models agree
    consensus_action: Action | None = None
    consensus_strike: float | None = None
    consensus_expiry: date | None = None
    consensus_confidence: float | None = None

    agreement_count: int = 0
    all_reasoning: list[str] = Field(default_factory=list)
    all_invalidating_conditions: list[str] = Field(default_factory=list)
    open_positions: list[OpenPosition] = Field(default_factory=list)

    # True when >= 2 models agree on a non-HOLD action — this is the gate for
    # the next pipeline stage
    passed: bool = False
