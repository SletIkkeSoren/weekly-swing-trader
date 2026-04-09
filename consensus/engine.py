"""
Consensus engine: fans out to all three models in parallel, validates schema,
aggregates votes. Requires >= 2/3 agreement on a non-HOLD action to pass.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone

from consensus.callers import gemini, mistral, openai_caller
from consensus.config import Config
from consensus.models import Action, ConsensusResult, ModelVote
from fetcher.models import MarketSnapshot

log = logging.getLogger(__name__)

_CALLERS = [mistral.call, gemini.call, openai_caller.call]
_REQUIRED_AGREEMENT = 2


async def evaluate(snapshot: MarketSnapshot, cfg: Config) -> ConsensusResult:
    """Run all model calls in parallel and return an aggregated ConsensusResult."""
    raw_votes: list[ModelVote | None] = await asyncio.gather(
        *[caller(snapshot, cfg) for caller in _CALLERS],
        return_exceptions=True,
    )

    votes = []
    rejected = 0
    for result in raw_votes:
        if isinstance(result, BaseException):
            log.warning("[%s] Model call raised exception: %s", snapshot.ticker, result)
            rejected += 1
        elif result is None:
            rejected += 1
        else:
            votes.append(result)
    if rejected:
        log.warning("[%s] %d model call(s) rejected (invalid schema or error)", snapshot.ticker, rejected)

    result = ConsensusResult(
        ticker=snapshot.ticker,
        as_of=datetime.now(timezone.utc),
        latest_close=snapshot.latest_close,
        votes=votes,
        all_reasoning=[r for v in votes for r in v.reasoning],
        all_invalidating_conditions=[c for v in votes for c in v.invalidating_conditions],
        open_positions=snapshot.open_positions,
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

    strike_str = f"{result.consensus_strike:.2f}" if result.consensus_strike else "n/a"
    log.info(
        "[%s] Consensus: %s  strike=%s  expiry=%s  confidence=%.2f  agreement=%d/3",
        snapshot.ticker,
        result.consensus_action,
        strike_str,
        result.consensus_expiry or "n/a",
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
