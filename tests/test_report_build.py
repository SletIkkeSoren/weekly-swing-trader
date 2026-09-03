"""
Tests for report/build.py — pure, no I/O. Feeds fake TickerStats/approved/
execution records straight into build_report and asserts on the resulting
Discord payload.
"""

from datetime import date, timedelta

from audit.performance import ClosedTrade, TickerStats
from report.build import build_report

_TODAY = date(2026, 8, 19)
_BASE_MIN_CONFIDENCE = 0.65


def _approved(ticker="TSLA", action="BUY_CALL", confidence=0.75, reasoning=None):
    return {
        "proposal": {
            "ticker": ticker,
            "action": action,
            "confidence": confidence,
            "all_reasoning": reasoning if reasoning is not None else ["momentum is bullish"],
        },
        "approved_at": "2026-08-19T22:00:00+00:00",
        "webhook_reason": "",
    }


def _trade(ticker, win, return_pct, closed_at) -> ClosedTrade:
    return ClosedTrade(
        ticker=ticker, occ_symbol="x", open_price=1.0,
        close_price=1.0 + return_pct, return_pct=return_pct, win=win, closed_at=closed_at,
    )


def _execution(ticker="TSLA", action="BUY_CALL", status="filled",
               filled_avg_price=3.20, contracts=1, reason=""):
    return {
        "ticker": ticker,
        "action": action,
        "status": status,
        "filled_avg_price": filled_avg_price,
        "contracts": contracts,
        "reason": reason,
    }


def _field(payload, name):
    return next((f for f in payload["embeds"][0]["fields"] if f["name"] == name), None)


def _field_startswith(payload, prefix):
    return next(
        (f for f in payload["embeds"][0]["fields"] if f["name"].startswith(prefix)), None
    )


class TestBuildReport:
    def test_no_activity_shows_placeholder(self):
        payload = build_report({}, [], [], _TODAY, _BASE_MIN_CONFIDENCE)
        field = _field(payload, "Trades today")
        assert field is not None
        assert "No approved trades" in field["value"]

    def test_approved_trade_shows_action_confidence_and_reasoning(self):
        approved = [_approved(ticker="TSLA", action="BUY_CALL", confidence=0.80,
                               reasoning=["breakout above resistance"])]
        payload = build_report({}, approved, [], _TODAY, _BASE_MIN_CONFIDENCE)
        field = _field(payload, "\U0001F4C8 TSLA — BUY_CALL")
        assert field is not None
        assert "80%" in field["value"]
        assert "breakout above resistance" in field["value"]

    def test_close_today_is_reported_with_outcome(self):
        stats = {"TSLA": TickerStats(ticker="TSLA", trades=[
            _trade("TSLA", True, 0.35, _TODAY.isoformat()),
        ])}
        payload = build_report(stats, [], [], _TODAY, _BASE_MIN_CONFIDENCE)
        field = _field(payload, "Closed today")
        assert field is not None
        assert "TSLA" in field["value"] and "+35%" in field["value"]

    def test_close_from_a_prior_day_is_not_reported_as_today(self):
        yesterday = (_TODAY - timedelta(days=1)).isoformat()
        stats = {"TSLA": TickerStats(ticker="TSLA", trades=[
            _trade("TSLA", True, 0.35, yesterday),
        ])}
        payload = build_report(stats, [], [], _TODAY, _BASE_MIN_CONFIDENCE)
        assert _field(payload, "Closed today") is None

    def test_adjusted_threshold_shown_once_track_record_is_established(self):
        trades = [
            _trade("TSLA", True, 1.0, "2026-08-01"),
            _trade("TSLA", False, -0.5, "2026-08-05"),
            _trade("TSLA", False, -0.5, "2026-08-10"),
        ]
        stats = {"TSLA": TickerStats(ticker="TSLA", trades=trades)}
        payload = build_report(stats, [], [], _TODAY, _BASE_MIN_CONFIDENCE)
        field = _field(payload, "Adjusted entry bar (track record)")
        assert field is not None
        assert "TSLA: 0.65" in field["value"]

    def test_threshold_field_absent_below_track_record_minimum(self):
        stats = {"TSLA": TickerStats(ticker="TSLA", trades=[
            _trade("TSLA", False, -0.5, "2026-08-10"),
        ])}
        payload = build_report(stats, [], [], _TODAY, _BASE_MIN_CONFIDENCE)
        assert _field(payload, "Adjusted entry bar (track record)") is None

    def test_week_over_week_trend_splits_correctly(self):
        this_week_day = (_TODAY - timedelta(days=2)).isoformat()
        last_week_day = (_TODAY - timedelta(days=9)).isoformat()
        stats = {"TSLA": TickerStats(ticker="TSLA", trades=[
            _trade("TSLA", True, 0.2, this_week_day),
            _trade("TSLA", True, 0.2, this_week_day),
            _trade("TSLA", False, -0.2, last_week_day),
        ])}
        payload = build_report(stats, [], [], _TODAY, _BASE_MIN_CONFIDENCE)
        field = _field(payload, "Win rate — this week vs last")
        assert field is not None
        assert "100% (2)" in field["value"]
        assert "0% (1)" in field["value"]


# ── Execution cross-check: an approval is not a trade ─────────────────────


class TestExecutionCrossCheck:
    def test_approved_but_nothing_executed_is_flagged(self):
        """The 2026-08-31 regression: 3 approvals, 0 orders, reported as trades."""
        payload = build_report({}, [_approved()], [], _TODAY, _BASE_MIN_CONFIDENCE)
        field = _field(payload, "\u26a0\ufe0f Orders at the broker")
        assert field is not None
        assert "executor recorded nothing" in field["value"]

    def test_rejected_orders_are_surfaced_with_reason(self):
        executions = [_execution(status="error", filled_avg_price=None,
                                 reason="HTTP 422: market is closed")]
        payload = build_report({}, [_approved()], executions, _TODAY, _BASE_MIN_CONFIDENCE)
        field = _field_startswith(payload, "\u26a0\ufe0f 1 order(s) never reached the market")
        assert field is not None
        assert "market is closed" in field["value"]

    def test_failed_orders_turn_the_embed_red(self):
        executions = [_execution(status="error", filled_avg_price=None, reason="rejected")]
        payload = build_report({}, [_approved()], executions, _TODAY, _BASE_MIN_CONFIDENCE)
        assert payload["embeds"][0]["color"] == 0xE74C3C

    def test_clean_run_counts_orders_that_reached_the_market(self):
        executions = [_execution(status="filled"), _execution(ticker="NVDA", status="submitted",
                                                              filled_avg_price=None)]
        payload = build_report({}, [_approved()], executions, _TODAY, _BASE_MIN_CONFIDENCE)
        field = _field_startswith(payload, "Orders at the broker")
        assert field is not None
        assert "2/2 reached the market" in field["name"]
        assert payload["embeds"][0]["color"] == 0x3498DB

    def test_skipped_order_is_reported_as_not_reaching_the_market(self):
        executions = [_execution(status="skipped", filled_avg_price=None,
                                 reason="no ask quote")]
        payload = build_report({}, [_approved()], executions, _TODAY, _BASE_MIN_CONFIDENCE)
        field = _field_startswith(payload, "Orders at the broker")
        assert "0/1 reached the market" in field["name"]
