"""
Tests for audit/performance.py — reconciling executions into closed trades and
rolling them up into the stats the gate's loss-cooldown filter relies on.
"""

from datetime import date, timedelta

import audit.performance as performance
from audit.performance import ClosedTrade, TickerStats, _reconcile, get_ticker_stats


def _execution(
    ticker="TSLA",
    action="BUY_CALL",
    occ_symbol="TSLA240119C00250000",
    filled_avg_price=3.20,
    status="submitted",
    executed_at="2026-01-01T00:00:00+00:00",
):
    return {
        "ticker": ticker,
        "action": action,
        "occ_symbol": occ_symbol,
        "filled_avg_price": filled_avg_price,
        "status": status,
        "executed_at": executed_at,
    }


# ── _reconcile ──────────────────────────────────────────────────────────────


class TestReconcile:
    def test_pairs_open_and_close_into_a_win(self):
        executions = [
            _execution(action="BUY_CALL", filled_avg_price=3.20, executed_at="2026-01-01T00:00:00+00:00"),
            _execution(action="CLOSE", filled_avg_price=6.40, executed_at="2026-01-05T00:00:00+00:00"),
        ]
        closed = _reconcile(executions)
        assert len(closed) == 1
        assert closed[0].win is True
        assert closed[0].return_pct == 1.0

    def test_pairs_open_and_close_into_a_loss(self):
        executions = [
            _execution(action="BUY_CALL", filled_avg_price=4.00, executed_at="2026-01-01T00:00:00+00:00"),
            _execution(action="CLOSE", filled_avg_price=2.00, executed_at="2026-01-05T00:00:00+00:00"),
        ]
        closed = _reconcile(executions)
        assert len(closed) == 1
        assert closed[0].win is False
        assert closed[0].return_pct == -0.5

    def test_close_without_matching_open_is_ignored(self):
        executions = [_execution(action="CLOSE", filled_avg_price=6.40)]
        assert _reconcile(executions) == []

    def test_open_without_close_yet_produces_no_closed_trade(self):
        executions = [_execution(action="BUY_CALL", filled_avg_price=3.20)]
        assert _reconcile(executions) == []

    def test_unfilled_open_is_never_paired(self):
        executions = [
            _execution(action="BUY_CALL", filled_avg_price=None),
            _execution(action="CLOSE", filled_avg_price=6.40),
        ]
        assert _reconcile(executions) == []

    def test_error_status_executions_are_skipped(self):
        executions = [
            _execution(action="BUY_CALL", filled_avg_price=3.20, status="error"),
            _execution(action="CLOSE", filled_avg_price=6.40),
        ]
        assert _reconcile(executions) == []

    def test_different_tickers_do_not_cross_pair(self):
        executions = [
            _execution(ticker="TSLA", occ_symbol="TSLA240119C00250000", action="BUY_CALL", filled_avg_price=3.0),
            _execution(ticker="NVDA", occ_symbol="NVDA240119C00500000", action="CLOSE", filled_avg_price=5.0),
        ]
        assert _reconcile(executions) == []

    def test_second_close_after_reopen_pairs_with_new_open(self):
        """Same occ_symbol traded twice — each CLOSE should pair with its own open."""
        executions = [
            _execution(action="BUY_CALL", filled_avg_price=2.00, executed_at="2026-01-01T00:00:00+00:00"),
            _execution(action="CLOSE", filled_avg_price=4.00, executed_at="2026-01-03T00:00:00+00:00"),
            _execution(action="BUY_CALL", filled_avg_price=1.00, executed_at="2026-01-05T00:00:00+00:00"),
            _execution(action="CLOSE", filled_avg_price=0.50, executed_at="2026-01-08T00:00:00+00:00"),
        ]
        closed = _reconcile(executions)
        assert len(closed) == 2
        assert closed[0].win is True
        assert closed[1].win is False


# ── TickerStats.loss_cooldown_active ────────────────────────────────────────


def _closed_trade(win: bool, closed_at: str) -> ClosedTrade:
    return ClosedTrade(
        ticker="TSLA", occ_symbol="x", open_price=1.0,
        close_price=2.0 if win else 0.5, return_pct=1.0 if win else -0.5,
        win=win, closed_at=closed_at,
    )


class TestLossCooldown:
    def test_fewer_trades_than_window_never_triggers(self):
        stats = TickerStats(ticker="TSLA", trades=[_closed_trade(False, "2026-08-10")])
        assert stats.loss_cooldown_active(3, 2, 14) is False

    def test_below_loss_threshold_does_not_trigger(self):
        trades = [
            _closed_trade(True, "2026-08-01"),
            _closed_trade(True, "2026-08-05"),
            _closed_trade(False, "2026-08-10"),
        ]
        stats = TickerStats(ticker="TSLA", trades=trades)
        assert stats.loss_cooldown_active(3, 2, 14) is False

    def test_meeting_threshold_within_window_triggers(self):
        trades = [
            _closed_trade(True, "2026-08-01"),
            _closed_trade(False, "2026-08-05"),
            _closed_trade(False, "2026-08-10"),
        ]
        stats = TickerStats(ticker="TSLA", trades=trades)
        today = date(2026, 8, 12)
        assert stats.loss_cooldown_active(3, 2, 14, today=today) is True

    def test_cooldown_expires_after_days_elapse(self):
        trades = [
            _closed_trade(False, "2026-07-01"),
            _closed_trade(False, "2026-07-05"),
            _closed_trade(True, "2026-07-10"),
        ]
        stats = TickerStats(ticker="TSLA", trades=trades)
        # 2 of last 3 are still losses, but the most recent trade closed 30d ago
        today = date(2026, 8, 9)
        assert stats.loss_cooldown_active(3, 2, 14, today=today) is False

    def test_only_last_window_trades_are_considered(self):
        """4 trades, window=3: only the most recent 3 count, oldest loss ignored."""
        trades = [
            _closed_trade(False, "2026-08-01"),
            _closed_trade(True, "2026-08-05"),
            _closed_trade(True, "2026-08-08"),
            _closed_trade(True, "2026-08-10"),
        ]
        stats = TickerStats(ticker="TSLA", trades=trades)
        today = date(2026, 8, 11)
        assert stats.loss_cooldown_active(3, 2, 14, today=today) is False


# ── get_ticker_stats ─────────────────────────────────────────────────────────


class TestGetTickerStats:
    def test_no_history_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(performance, "load_stage", lambda stage, since: [])
        assert get_ticker_stats() == {}

    def test_groups_closed_trades_by_ticker(self, monkeypatch):
        executions = [
            _execution(ticker="TSLA", occ_symbol="A", action="BUY_CALL", filled_avg_price=2.0, executed_at="2026-01-01T00:00:00+00:00"),
            _execution(ticker="TSLA", occ_symbol="A", action="CLOSE", filled_avg_price=4.0, executed_at="2026-01-03T00:00:00+00:00"),
            _execution(ticker="NVDA", occ_symbol="B", action="BUY_PUT", filled_avg_price=5.0, executed_at="2026-01-01T00:00:00+00:00"),
            _execution(ticker="NVDA", occ_symbol="B", action="CLOSE", filled_avg_price=1.0, executed_at="2026-01-03T00:00:00+00:00"),
        ]
        monkeypatch.setattr(performance, "load_stage", lambda stage, since: executions)
        stats = get_ticker_stats()
        assert set(stats) == {"TSLA", "NVDA"}
        assert stats["TSLA"].win_rate == 1.0
        assert stats["NVDA"].win_rate == 0.0
