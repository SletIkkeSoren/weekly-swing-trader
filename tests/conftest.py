"""Shared fixtures for the test suite."""

from datetime import date, datetime, timedelta, timezone

import pytest

from consensus.models import ConsensusResult, ModelVote
from fetcher.models import MarketSnapshot, OHLCVBar, OpenPosition, TechnicalIndicators
from gate.config import Config as GateConfig


# ── Bar / Snapshot helpers ─────────────────────────────────────────────────


def make_bars(n: int = 252, base_price: float = 100.0) -> list[OHLCVBar]:
    """Synthetic daily bars with a gentle uptrend — enough history for EMA-200."""
    bars = []
    start = date(2025, 1, 1)
    price = base_price
    for i in range(n):
        price *= 1.001
        bars.append(
            OHLCVBar(
                date=start + timedelta(days=i),
                open=round(price * 0.999, 4),
                high=round(price * 1.005, 4),
                low=round(price * 0.995, 4),
                close=round(price, 4),
                volume=1_000_000.0,
            )
        )
    return bars


def make_indicators(close: float = 150.0) -> TechnicalIndicators:
    return TechnicalIndicators(
        ema_8=close * 1.01,
        ema_21=close * 1.005,
        ema_50=close * 0.99,
        ema_200=close * 0.95,
        rsi_14=55.0,
        macd=0.5,
        macd_signal=0.3,
        macd_hist=0.2,
        bb_upper=close * 1.03,
        bb_mid=close,
        bb_lower=close * 0.97,
        atr_14=2.5,
        hv_20=0.25,
        volume_ratio=1.2,
        macd_hist_slope=0.01,
        price_pct_90d=0.65,
        nearest_support=close * 0.98,
        nearest_resistance=close * 1.02,
    )


def make_snapshot(
    ticker: str = "TSLA",
    latest_close: float = 150.0,
    open_positions: list[OpenPosition] | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        ticker=ticker,
        as_of=datetime.now(timezone.utc),
        latest_close=latest_close,
        bars=make_bars(),
        indicators=make_indicators(latest_close),
        open_positions=open_positions or [],
    )


# ── Position helper ────────────────────────────────────────────────────────


def make_position(
    ticker: str = "TSLA",
    pnl_pct: float = 0.10,
    days_to_expiry: int = 10,
    occ_symbol: str = "TSLA240119C00250000",
    qty: int = 2,
    avg_entry_price: float = 3.20,
) -> OpenPosition:
    return OpenPosition(
        ticker=ticker,
        occ_symbol=occ_symbol,
        option_type="call",
        qty=qty,
        avg_entry_price=avg_entry_price,
        current_price=round(avg_entry_price * (1 + pnl_pct), 4),
        pnl_pct=pnl_pct,
        days_to_expiry=days_to_expiry,
    )


# ── ConsensusResult helper ─────────────────────────────────────────────────


def make_vote(
    model: str = "claude",
    action: str = "BUY_CALL",
    strike: float = 150.0,
    confidence: float = 0.75,
    expiry: date | None = None,
) -> ModelVote:
    exp = expiry or date(2026, 4, 3)
    return ModelVote(
        model=model,
        action=action,
        strike=strike if action not in ("HOLD", "CLOSE") else None,
        expiry=exp if action not in ("HOLD", "CLOSE") else None,
        confidence=confidence,
        reasoning=["momentum is bullish"],
        invalidating_conditions=["RSI drops below 40"],
    )


def make_consensus(
    action: str = "BUY_CALL",
    strike: float = 150.0,
    confidence: float = 0.75,
    agreement_count: int = 2,
    latest_close: float = 150.0,
    passed: bool = True,
    open_positions: list[OpenPosition] | None = None,
) -> ConsensusResult:
    exp = date(2026, 4, 3)
    return ConsensusResult(
        ticker="TSLA",
        as_of=datetime.now(timezone.utc),
        latest_close=latest_close,
        votes=[],
        consensus_action=action,
        consensus_strike=strike if action not in ("HOLD", "CLOSE") else None,
        consensus_expiry=exp if action not in ("HOLD", "CLOSE") else None,
        consensus_confidence=confidence,
        agreement_count=agreement_count,
        passed=passed,
        open_positions=open_positions or [],
    )


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def gate_cfg() -> GateConfig:
    return GateConfig(
        input_dir="/tmp/consensus",
        output_dir="/tmp/gate",
        min_confidence=0.65,
        min_agreement=2,
        max_open_trades=3,
        account_size=10_000.0,
        risk_pct=0.01,
        max_contracts=5,
        webhook_url="",
        webhook_timeout_secs=30,
    )


@pytest.fixture
def sample_snapshot() -> MarketSnapshot:
    return make_snapshot()
