"""
Risk filters applied before a trade is sent for human approval.
Each filter returns a FilterResult — first failure short-circuits the chain.

Hard exit rules (check_hard_exits) fire unconditionally on open positions,
bypassing the normal consensus flow entirely.
"""

import math

from audit.performance import TickerStats
from consensus.models import ConsensusResult
from fetcher.models import OpenPosition
from gate.config import Config
from gate.models import FilterResult

# Hard exit thresholds
_STOP_LOSS_PCT = -0.25    # force CLOSE if down 25% or more
_TAKE_PROFIT_PCT = 1.50   # force CLOSE if up 150% or more
_MIN_DTE = 7              # force CLOSE if 7 or fewer days to expiry

# Dynamic confidence threshold — track-record-adjusted bar for new entries
_TRACK_RECORD_MIN_TRADES = 3   # need at least this many closed trades before adjusting
_WIN_RATE_WEIGHT = 0.4         # +/- swing in threshold from win rate alone
_RETURN_WEIGHT = 0.5           # additional swing from avg return magnitude
_THRESHOLD_FLOOR = 0.35        # never relax the bar below this, however good the streak
_THRESHOLD_CEILING = 1.01      # push just past 1.0 so no confidence score can clear it


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


def run_all(
    result: ConsensusResult,
    cfg: Config,
    open_trade_count: int,
    ticker_stats: dict[str, TickerStats] | None = None,
) -> FilterResult:
    # CLOSE actions skip strike sanity, open-trade cap, and the track-record
    # adjustment (they reduce exposure, and a bad streak should never block an exit)
    if result.consensus_action == "CLOSE":
        for check in (_confidence, _agreement):
            fr = check(result, cfg, open_trade_count)
            if not fr.passed:
                return fr
        return FilterResult(passed=True, reason="ok")

    fr = _confidence(result, cfg, open_trade_count, ticker_stats)
    if not fr.passed:
        return fr

    for check in (_agreement, _open_trades, _strike_sanity):
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

def dynamic_confidence_threshold(base_min_confidence: float, stats: TickerStats | None) -> float:
    """Track-record-adjusted confidence bar for a new entry on this ticker.

    No history (or too little to be meaningful) -> base threshold, unchanged.
    A ticker on a bad recent run raises the bar; a ticker on a good run lowers it
    (never below the floor). A bad enough streak pushes the threshold above 1.0,
    which subsumes the old hard cooldown as its extreme case instead of a separate
    binary rule. Models never see this — it's computed entirely from this account's
    own execution history (see CLAUDE.md core rules)."""
    if stats is None or stats.n < _TRACK_RECORD_MIN_TRADES:
        return base_min_confidence
    win_rate = stats.win_rate or 0.0
    avg_return = stats.avg_return_pct or 0.0
    skew = (0.5 - win_rate) * _WIN_RATE_WEIGHT * 2
    return_adj = -avg_return * _RETURN_WEIGHT
    threshold = base_min_confidence + skew + return_adj
    return max(_THRESHOLD_FLOOR, min(_THRESHOLD_CEILING, threshold))


def _confidence(
    result: ConsensusResult,
    cfg: Config,
    _: int,
    ticker_stats: dict[str, TickerStats] | None = None,
) -> FilterResult:
    stats = ticker_stats.get(result.ticker) if ticker_stats else None
    threshold = dynamic_confidence_threshold(cfg.min_confidence, stats)
    confidence = result.consensus_confidence or 0.0
    if confidence < threshold:
        detail = "" if threshold == cfg.min_confidence else f" (base {cfg.min_confidence}, track-record adjusted)"
        return FilterResult(
            passed=False,
            reason=f"confidence {confidence:.2f} < min {threshold:.2f}{detail}",
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
