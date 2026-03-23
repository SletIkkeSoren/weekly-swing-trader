"""
Tests for consensus/engine.py — vote aggregation logic.

All model callers are replaced with AsyncMocks so no real API calls are made.
"""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from consensus.engine import evaluate
from consensus.models import ConsensusResult, ModelVote
from fetcher.models import MarketSnapshot

from tests.conftest import make_snapshot, make_vote


# ── Helpers ────────────────────────────────────────────────────────────────


def _callers(*votes: ModelVote | None):
    """Return a list of AsyncMocks that yield the given votes in order."""
    return [AsyncMock(return_value=v) for v in votes]


async def _evaluate(snapshot: MarketSnapshot, votes: list[ModelVote | None]) -> ConsensusResult:
    with patch("consensus.engine._CALLERS", _callers(*votes)):
        from consensus.config import Config as ConsensusConfig
        cfg = ConsensusConfig(
            anthropic_api_key="test",
            google_api_key="test",
            claude_model="claude-haiku-4-5-20251001",
            gemini_model="gemini-2.5-flash-lite",
            gemini_model_2="gemini-2.5-flash",
            input_dir="/tmp",
            output_dir="/tmp",
        )
        return await evaluate(snapshot, cfg)


# ── Consensus passing ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unanimous_buy_call_passes():
    votes = [
        make_vote("claude", "BUY_CALL", strike=150.0, confidence=0.80),
        make_vote("gemini", "BUY_CALL", strike=152.0, confidence=0.70),
        make_vote("gemini2", "BUY_CALL", strike=154.0, confidence=0.60),
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.passed is True
    assert result.consensus_action == "BUY_CALL"
    assert result.agreement_count == 3
    assert result.consensus_confidence == pytest.approx((0.80 + 0.70 + 0.60) / 3)
    assert result.consensus_strike == pytest.approx((150.0 + 152.0 + 154.0) / 3)


@pytest.mark.asyncio
async def test_two_of_three_agreement_passes():
    votes = [
        make_vote("claude", "BUY_CALL", strike=150.0, confidence=0.80),
        make_vote("gemini", "BUY_CALL", strike=152.0, confidence=0.70),
        make_vote("gemini2", "BUY_PUT", strike=148.0, confidence=0.60),
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.passed is True
    assert result.consensus_action == "BUY_CALL"
    assert result.agreement_count == 2
    # confidence/strike only from the two agreeing voters
    assert result.consensus_confidence == pytest.approx((0.80 + 0.70) / 2)
    assert result.consensus_strike == pytest.approx((150.0 + 152.0) / 2)


@pytest.mark.asyncio
async def test_two_of_three_buy_put_passes():
    votes = [
        make_vote("claude", "BUY_PUT", strike=145.0, confidence=0.75),
        make_vote("gemini", "BUY_PUT", strike=143.0, confidence=0.65),
        make_vote("gemini2", "HOLD", confidence=0.50),
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.passed is True
    assert result.consensus_action == "BUY_PUT"


# ── Consensus failing ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_three_way_split_does_not_pass():
    votes = [
        make_vote("claude", "BUY_CALL", confidence=0.80),
        make_vote("gemini", "BUY_PUT", confidence=0.70),
        make_vote("gemini2", "HOLD", confidence=0.60),
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.passed is False
    assert result.consensus_action is None
    assert result.agreement_count == 0


@pytest.mark.asyncio
async def test_unanimous_hold_does_not_pass():
    """HOLD agreement never sets passed=True — it's not a tradeable action."""
    votes = [
        make_vote("claude", "HOLD", confidence=0.90),
        make_vote("gemini", "HOLD", confidence=0.85),
        make_vote("gemini2", "HOLD", confidence=0.80),
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.passed is False
    assert result.consensus_action == "HOLD"
    assert result.agreement_count == 3


@pytest.mark.asyncio
async def test_two_hold_one_buy_call_does_not_pass():
    votes = [
        make_vote("claude", "HOLD", confidence=0.80),
        make_vote("gemini", "HOLD", confidence=0.75),
        make_vote("gemini2", "BUY_CALL", confidence=0.70),
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.passed is False
    assert result.consensus_action == "HOLD"


# ── None / error votes ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_none_vote_still_passes_with_two_agreement():
    votes = [
        make_vote("claude", "BUY_CALL", strike=150.0, confidence=0.80),
        make_vote("gemini", "BUY_CALL", strike=152.0, confidence=0.70),
        None,  # gemini2 errored out
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.passed is True
    assert result.agreement_count == 2


@pytest.mark.asyncio
async def test_two_none_votes_does_not_pass():
    votes = [
        make_vote("claude", "BUY_CALL", strike=150.0, confidence=0.80),
        None,
        None,
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.passed is False
    assert result.agreement_count == 0


@pytest.mark.asyncio
async def test_all_none_votes_returns_empty_result():
    result = await _evaluate(make_snapshot(), [None, None, None])

    assert result.passed is False
    assert result.votes == []
    assert result.consensus_action is None


# ── Strike / expiry aggregation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consensus_strike_is_average_of_agreeing_votes():
    votes = [
        make_vote("claude", "BUY_CALL", strike=100.0, confidence=0.80),
        make_vote("gemini", "BUY_CALL", strike=110.0, confidence=0.70),
        make_vote("gemini2", "HOLD", confidence=0.60),  # disagrees
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.consensus_strike == pytest.approx(105.0)


@pytest.mark.asyncio
async def test_consensus_expiry_is_max_of_agreeing_votes():
    votes = [
        make_vote("claude", "BUY_CALL", expiry=date(2026, 3, 27), confidence=0.80),
        make_vote("gemini", "BUY_CALL", expiry=date(2026, 4, 3), confidence=0.70),
        make_vote("gemini2", "HOLD", confidence=0.60),
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.consensus_expiry == date(2026, 4, 3)


@pytest.mark.asyncio
async def test_close_action_sets_passed_true():
    votes = [
        make_vote("claude", "CLOSE", confidence=0.80),
        make_vote("gemini", "CLOSE", confidence=0.70),
        make_vote("gemini2", "HOLD", confidence=0.50),
    ]
    result = await _evaluate(make_snapshot(), votes)

    assert result.passed is True
    assert result.consensus_action == "CLOSE"
    assert result.consensus_strike is None


# ── Result metadata ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ticker_and_close_propagated_to_result():
    snapshot = make_snapshot(ticker="NVDA", latest_close=800.0)
    votes = [
        make_vote("claude", "BUY_CALL", strike=800.0, confidence=0.80),
        make_vote("gemini", "BUY_CALL", strike=805.0, confidence=0.75),
        make_vote("gemini2", "HOLD", confidence=0.50),
    ]
    result = await _evaluate(snapshot, votes)

    assert result.ticker == "NVDA"
    assert result.latest_close == 800.0


@pytest.mark.asyncio
async def test_all_reasoning_collected_from_all_valid_votes():
    votes = [
        make_vote("claude", "BUY_CALL", confidence=0.80),
        make_vote("gemini", "BUY_PUT", confidence=0.70),
        make_vote("gemini2", "HOLD", confidence=0.60),
    ]
    result = await _evaluate(make_snapshot(), votes)

    # 3 votes × 1 reasoning entry each
    assert len(result.all_reasoning) == 3
