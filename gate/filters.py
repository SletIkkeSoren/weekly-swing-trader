"""
Risk filters applied before a trade is sent for human approval.
Each filter returns a FilterResult — first failure short-circuits the chain.

Hard exit rules (check_hard_exits) fire unconditionally on open positions,
bypassing the normal consensus flow entirely.
"""

import math

from consensus.models import ConsensusResult
from fetcher.models import OpenPosition
from gate.config import Config
from gate.models import FilterResult

# Hard exit thresholds
_STOP_LOSS_PCT = -0.50    # force CLOSE if down 50% or more
_TAKE_PROFIT_PCT = 1.50   # force CLOSE if up 150% or more
_MIN_DTE = 2              # force CLOSE if 2 or fewer days to expiry


def check_hard_exits(
    open_positions: list[OpenPosition], cfg: Config
) -> list[tuple[OpenPosition, str]]:
    """Return (position, reason) pairs for positions that must be closed immediately.

    These rules fire regardless of model consensus.
    """
    forced: list[tuple[OpenPosition, str]] = []
    for pos in open_positions:
        if pos.pnl_pct <= _STOP_LOSS_PCT:
            forced.append((pos, f"stop-loss triggered: down {pos.pnl_pct:.0%}"))
        elif pos.days_to_expiry <= _MIN_DTE:
            forced.append((pos, f"expiry risk: {pos.days_to_expiry}d to expiry"))
        elif pos.pnl_pct >= _TAKE_PROFIT_PCT:
            forced.append((pos, f"take-profit triggered: up {pos.pnl_pct:.0%}"))
    return forced


def run_all(result: ConsensusResult, cfg: Config, open_trade_count: int) -> FilterResult:
    # CLOSE actions skip strike sanity and open-trade cap (they reduce exposure)
    if result.consensus_action == "CLOSE":
        for check in (_confidence, _agreement):
            fr = check(result, cfg, open_trade_count)
            if not fr.passed:
                return fr
        return FilterResult(passed=True, reason="ok")

    for check in (_confidence, _agreement, _open_trades, _strike_sanity):
        fr = check(result, cfg, open_trade_count)
        if not fr.passed:
            return fr
    return FilterResult(passed=True, reason="ok")


def suggested_contracts(cfg: Config) -> int:
    """
    Risk-based sizing: risk at most (account_size * risk_pct) per trade.
    Conservative floor of $200/contract; executor refines against actual option chain.
    Clamped to [1, max_contracts].
    """
    risk_usd = cfg.account_size * cfg.risk_pct
    raw = math.floor(risk_usd / 200)
    return max(1, min(raw, cfg.max_contracts))


# ── Individual filters ─────────────────────────────────────────────────────

def _confidence(result: ConsensusResult, cfg: Config, _: int) -> FilterResult:
    confidence = result.consensus_confidence or 0.0
    if confidence < cfg.min_confidence:
        return FilterResult(
            passed=False,
            reason=f"confidence {confidence:.2f} < min {cfg.min_confidence}",
        )
    return FilterResult(passed=True, reason="ok")


def _agreement(result: ConsensusResult, cfg: Config, _: int) -> FilterResult:
    if result.agreement_count < cfg.min_agreement:
        return FilterResult(
            passed=False,
            reason=f"agreement {result.agreement_count} < min {cfg.min_agreement}",
        )
    return FilterResult(passed=True, reason="ok")


def _open_trades(result: ConsensusResult, cfg: Config, open_trade_count: int) -> FilterResult:
    if open_trade_count >= cfg.max_open_trades:
        return FilterResult(
            passed=False,
            reason=f"open trades {open_trade_count} >= max {cfg.max_open_trades}",
        )
    return FilterResult(passed=True, reason="ok")


def _strike_sanity(result: ConsensusResult, cfg: Config, _: int) -> FilterResult:
    """Reject if the consensus strike is more than 15% away from current close."""
    if result.consensus_strike is None:
        return FilterResult(passed=False, reason="no strike price in consensus")
    close = result.latest_close
    deviation = abs(result.consensus_strike - close) / close
    if deviation > 0.15:
        return FilterResult(
            passed=False,
            reason=f"strike {result.consensus_strike} is {deviation:.1%} from close {close} (>15%)",
        )
    return FilterResult(passed=True, reason="ok")
