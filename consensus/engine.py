"""
Consensus engine: fans out to all three models in parallel, validates schema,
aggregates votes. Requires >= 2/3 agreement on a non-HOLD action to pass.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone

from consensus.callers import claude, gemini, gemini2
from consensus.config import Config
from consensus.models import Action, ConsensusResult, ModelVote
from fetcher.models import MarketSnapshot

log = logging.getLogger(__name__)

_CALLERS = [claude.call, gemini.call, gemini2.call]
_REQUIRED_AGREEMENT = 2


async def evaluate(snapshot: MarketSnapshot, cfg: Config) -> ConsensusResult:
    """Run all model calls in parallel and return an aggregated ConsensusResult."""
    raw_votes: list[ModelVote | None] = await asyncio.gather(
        *[caller(snapshot, cfg) for caller in _CALLERS],
        return_exceptions=False,
    )

    votes = [v for v in raw_votes if v is not None]
    rejected = len(raw_votes) - len(votes)
    if rejected:
        log.warning("[%s] %d model call(s) rejected (invalid schema or error)", snapshot.ticker, rejected)

    result = ConsensusResult(
        ticker=snapshot.ticker,
        as_of=datetime.now(timezone.utc),
        latest_close=snapshot.latest_close,
        votes=votes,
        all_reasoning=[r for v in votes for r in v.reasoning],
        all_invalidating_conditions=[c for v in votes for c in v.invalidating_conditions],
    )

    if not votes:
        log.warning("[%s] No valid votes — skipping", snapshot.ticker)
        return result

    action_counts: Counter[Action] = Counter(v.action for v in votes)
    top_action, top_count = action_counts.most_common(1)[0]

    if top_count < _REQUIRED_AGREEMENT:
        log.info("[%s] No consensus (votes: %s)", snapshot.ticker, dict(action_counts))
        return result

    agreeing = [v for v in votes if v.action == top_action]
    result.agreement_count = top_count
    result.consensus_action = top_action
    result.consensus_confidence = sum(v.confidence for v in agreeing) / len(agreeing)

    if top_action != "HOLD":
        strikes = [v.strike for v in agreeing if v.strike is not None]
        expiries = [v.expiry for v in agreeing if v.expiry is not None]
        result.consensus_strike = sum(strikes) / len(strikes) if strikes else None
        result.consensus_expiry = max(expiries) if expiries else None
        result.passed = True

    log.info(
        "[%s] Consensus: %s  strike=%.2f  expiry=%s  confidence=%.2f  agreement=%d/3",
        snapshot.ticker,
        result.consensus_action,
        result.consensus_strike or 0,
        result.consensus_expiry,
        result.consensus_confidence or 0,
        result.agreement_count,
    )
    return result


async def evaluate_all(snapshots: list[MarketSnapshot], cfg: Config) -> list[ConsensusResult]:
    """Evaluate tickers one at a time to respect free-tier rate limits.

    Within each ticker the three model calls still fire in parallel — the
    sequential loop only throttles how many tickers are in-flight at once.
    """
    results: list[ConsensusResult] = []
    for snapshot in snapshots:
        results.append(await evaluate(snapshot, cfg))
    return results
