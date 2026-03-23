"""
Tests for gate/filters.py — the most safety-critical module.

Every hard-exit boundary, every filter in the chain, and the position-sizing
formula are covered here. These tests do not touch any external API.
"""

import pytest

from gate.filters import check_hard_exits, run_all, suggested_contracts
from gate.config import Config as GateConfig

from tests.conftest import make_consensus, make_position


# ── check_hard_exits ───────────────────────────────────────────────────────


class TestHardExits:
    def test_healthy_position_is_ignored(self, gate_cfg):
        pos = make_position(pnl_pct=0.10, days_to_expiry=10)
        assert check_hard_exits([pos], gate_cfg) == []

    # Stop-loss boundary
    def test_stop_loss_exactly_50pct_down(self, gate_cfg):
        pos = make_position(pnl_pct=-0.50)
        exits = check_hard_exits([pos], gate_cfg)
        assert len(exits) == 1
        assert "stop-loss" in exits[0][1]

    def test_stop_loss_more_than_50pct_down(self, gate_cfg):
        pos = make_position(pnl_pct=-0.75)
        exits = check_hard_exits([pos], gate_cfg)
        assert len(exits) == 1
        assert "stop-loss" in exits[0][1]

    def test_just_under_stop_loss_does_not_trigger(self, gate_cfg):
        pos = make_position(pnl_pct=-0.49)
        assert check_hard_exits([pos], gate_cfg) == []

    # Take-profit boundary
    def test_take_profit_exactly_150pct_up(self, gate_cfg):
        pos = make_position(pnl_pct=1.50)
        exits = check_hard_exits([pos], gate_cfg)
        assert len(exits) == 1
        assert "take-profit" in exits[0][1]

    def test_take_profit_above_150pct(self, gate_cfg):
        pos = make_position(pnl_pct=2.00)
        exits = check_hard_exits([pos], gate_cfg)
        assert len(exits) == 1
        assert "take-profit" in exits[0][1]

    def test_just_under_take_profit_does_not_trigger(self, gate_cfg):
        pos = make_position(pnl_pct=1.49)
        assert check_hard_exits([pos], gate_cfg) == []

    # DTE boundary
    def test_dte_exactly_2_triggers(self, gate_cfg):
        pos = make_position(pnl_pct=0.05, days_to_expiry=2)
        exits = check_hard_exits([pos], gate_cfg)
        assert len(exits) == 1
        assert "expiry" in exits[0][1]

    def test_dte_1_triggers(self, gate_cfg):
        pos = make_position(pnl_pct=0.05, days_to_expiry=1)
        exits = check_hard_exits([pos], gate_cfg)
        assert len(exits) == 1

    def test_dte_3_does_not_trigger(self, gate_cfg):
        pos = make_position(pnl_pct=0.05, days_to_expiry=3)
        assert check_hard_exits([pos], gate_cfg) == []

    # elif priority: stop-loss fires before DTE check
    def test_stop_loss_takes_priority_over_dte(self, gate_cfg):
        """Position down 60% AND DTE=1 — stop-loss reason wins (elif chain)."""
        pos = make_position(pnl_pct=-0.60, days_to_expiry=1)
        exits = check_hard_exits([pos], gate_cfg)
        assert len(exits) == 1
        assert "stop-loss" in exits[0][1]

    # Multiple positions
    def test_multiple_positions_multiple_exits(self, gate_cfg):
        positions = [
            make_position(pnl_pct=-0.60, occ_symbol="TSLA240119C00200000"),  # stop-loss
            make_position(pnl_pct=0.10, days_to_expiry=5, occ_symbol="TSLA240119C00210000"),  # healthy
            make_position(pnl_pct=2.00, occ_symbol="TSLA240119C00220000"),   # take-profit
        ]
        exits = check_hard_exits(positions, gate_cfg)
        assert len(exits) == 2
        reasons = [r for _, r in exits]
        assert any("stop-loss" in r for r in reasons)
        assert any("take-profit" in r for r in reasons)

    def test_empty_positions_returns_empty(self, gate_cfg):
        assert check_hard_exits([], gate_cfg) == []

    def test_exit_tuple_contains_position_object(self, gate_cfg):
        pos = make_position(pnl_pct=-0.60)
        exits = check_hard_exits([pos], gate_cfg)
        assert exits[0][0] is pos


# ── suggested_contracts ────────────────────────────────────────────────────


class TestSuggestedContracts:
    def test_small_account_floors_at_1(self):
        cfg = GateConfig(
            input_dir="", output_dir="",
            min_confidence=0.65, min_agreement=2, max_open_trades=3,
            account_size=1_000.0, risk_pct=0.01,  # risk = $10 → 10/200 = 0 → floor to 1
            max_contracts=5, webhook_url="", webhook_timeout_secs=30,
        )
        assert suggested_contracts(cfg) == 1

    def test_standard_sizing(self):
        cfg = GateConfig(
            input_dir="", output_dir="",
            min_confidence=0.65, min_agreement=2, max_open_trades=3,
            account_size=10_000.0, risk_pct=0.05,  # risk = $500 → 500/200 = 2.5 → floor = 2
            max_contracts=5, webhook_url="", webhook_timeout_secs=30,
        )
        assert suggested_contracts(cfg) == 2

    def test_capped_at_max_contracts(self):
        cfg = GateConfig(
            input_dir="", output_dir="",
            min_confidence=0.65, min_agreement=2, max_open_trades=3,
            account_size=100_000.0, risk_pct=0.10,  # risk = $10000 → 50 raw → capped
            max_contracts=5, webhook_url="", webhook_timeout_secs=30,
        )
        assert suggested_contracts(cfg) == 5

    def test_exact_200_risk_gives_1_contract(self):
        cfg = GateConfig(
            input_dir="", output_dir="",
            min_confidence=0.65, min_agreement=2, max_open_trades=3,
            account_size=20_000.0, risk_pct=0.01,  # risk = $200 → 200/200 = 1
            max_contracts=5, webhook_url="", webhook_timeout_secs=30,
        )
        assert suggested_contracts(cfg) == 1


# ── run_all — BUY_CALL path ────────────────────────────────────────────────


class TestRunAllBuyCall:
    def test_passes_all_filters(self, gate_cfg):
        result = make_consensus(action="BUY_CALL", strike=150.0, confidence=0.75,
                                agreement_count=2, latest_close=150.0)
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is True

    def test_fails_on_low_confidence(self, gate_cfg):
        result = make_consensus(action="BUY_CALL", confidence=0.50)  # below 0.65 min
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is False
        assert "confidence" in fr.reason

    def test_fails_on_exact_min_confidence_boundary(self, gate_cfg):
        """Confidence exactly at min threshold should pass (not strictly less-than)."""
        result = make_consensus(action="BUY_CALL", confidence=0.65)
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is True

    def test_fails_on_low_agreement(self, gate_cfg):
        result = make_consensus(action="BUY_CALL", confidence=0.75, agreement_count=1)
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is False
        assert "agreement" in fr.reason

    def test_fails_when_at_max_open_trades(self, gate_cfg):
        result = make_consensus(action="BUY_CALL", confidence=0.75, agreement_count=2)
        fr = run_all(result, gate_cfg, open_trade_count=3)  # max_open_trades=3
        assert fr.passed is False
        assert "open trades" in fr.reason

    def test_passes_just_under_max_open_trades(self, gate_cfg):
        result = make_consensus(action="BUY_CALL", confidence=0.75, agreement_count=2,
                                strike=150.0, latest_close=150.0)
        fr = run_all(result, gate_cfg, open_trade_count=2)  # 2 < 3
        assert fr.passed is True

    def test_fails_on_strike_too_far_above_close(self, gate_cfg):
        # strike 200 vs close 150 → deviation = 50/150 = 33% > 15%
        result = make_consensus(action="BUY_CALL", strike=200.0, latest_close=150.0,
                                confidence=0.75, agreement_count=2)
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is False
        assert "strike" in fr.reason

    def test_fails_on_strike_too_far_below_close(self, gate_cfg):
        # strike 100 vs close 150 → deviation = 50/150 = 33% > 15%
        result = make_consensus(action="BUY_CALL", strike=100.0, latest_close=150.0,
                                confidence=0.75, agreement_count=2)
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is False

    def test_passes_strike_exactly_at_15pct_boundary(self, gate_cfg):
        # strike exactly 15% above close: 150 * 1.15 = 172.5
        result = make_consensus(action="BUY_CALL", strike=172.5, latest_close=150.0,
                                confidence=0.75, agreement_count=2)
        fr = run_all(result, gate_cfg, open_trade_count=0)
        # deviation = 22.5/150 = 0.15 — NOT > 0.15, so should pass
        assert fr.passed is True

    def test_fails_on_missing_strike(self, gate_cfg):
        from consensus.models import ConsensusResult
        from datetime import datetime, timezone
        result = ConsensusResult(
            ticker="TSLA",
            as_of=datetime.now(timezone.utc),
            latest_close=150.0,
            votes=[],
            consensus_action="BUY_CALL",
            consensus_strike=None,  # missing
            consensus_confidence=0.75,
            agreement_count=2,
            passed=True,
        )
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is False
        assert "strike" in fr.reason

    def test_short_circuits_on_first_failure(self, gate_cfg):
        """Confidence fails first — reason should mention confidence, not agreement."""
        result = make_consensus(action="BUY_CALL", confidence=0.40, agreement_count=1)
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is False
        assert "confidence" in fr.reason
        assert "agreement" not in fr.reason


# ── run_all — CLOSE path ───────────────────────────────────────────────────


class TestRunAllClose:
    def test_close_passes_without_strike(self, gate_cfg):
        result = make_consensus(action="CLOSE", strike=None, confidence=0.75,
                                agreement_count=2)
        fr = run_all(result, gate_cfg, open_trade_count=10)  # many open trades — ignored
        assert fr.passed is True

    def test_close_skips_open_trade_cap(self, gate_cfg):
        """CLOSE should pass even when open_trade_count >= max_open_trades."""
        result = make_consensus(action="CLOSE", confidence=0.75, agreement_count=2)
        fr = run_all(result, gate_cfg, open_trade_count=100)
        assert fr.passed is True

    def test_close_skips_strike_sanity(self, gate_cfg):
        """CLOSE has no strike, so strike sanity must not fire."""
        result = make_consensus(action="CLOSE", confidence=0.75, agreement_count=2)
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is True

    def test_close_still_requires_min_confidence(self, gate_cfg):
        result = make_consensus(action="CLOSE", confidence=0.40, agreement_count=2)
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is False
        assert "confidence" in fr.reason

    def test_close_still_requires_min_agreement(self, gate_cfg):
        result = make_consensus(action="CLOSE", confidence=0.75, agreement_count=1)
        fr = run_all(result, gate_cfg, open_trade_count=0)
        assert fr.passed is False
        assert "agreement" in fr.reason
