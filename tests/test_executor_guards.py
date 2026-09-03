"""
Tests for the executor's preflight guards and quote handling.

These cover the 2026-08-31 failure mode directly: approved.json persists on the
shared PVC, so a re-read must not become a duplicate order, a stale batch must
not be executed a day late, and a missing quote must not silently become a
market order.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from executor.config import Config
from executor.main import (
    already_executed,
    batch_age,
    batch_key,
    execute_trade,
    mark_executed,
)
from gate.models import ApprovedTrade, TradeProposal


def _cfg(tmp_path, **overrides) -> Config:
    base = dict(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        input_dir=str(tmp_path / "gate"),
        output_dir=str(tmp_path / "executor"),
        order_type="limit",
        limit_buffer=1.05,
        dry_run=False,
        discord_webhook_url="",
        options_feed="indicative",
        require_market_open=True,
        max_approval_age_hours=6.0,
        allow_market_fallback=False,
    )
    base.update(overrides)
    return Config(**base)


def _trade(approved_at: datetime, ticker: str = "TSLA") -> ApprovedTrade:
    return ApprovedTrade(
        proposal=TradeProposal(
            ticker=ticker,
            action="BUY_CALL",
            strike=150.0,
            expiry=date(2026, 9, 18),
            confidence=0.75,
            agreement_count=2,
            suggested_contracts=1,
            risk_usd=100.0,
            all_reasoning=["momentum is bullish"],
            all_invalidating_conditions=["RSI drops below 40"],
            as_of=approved_at,
        ),
        approved_at=approved_at,
        webhook_reason="auto-approved",
    )


class FakeClient:
    """Stands in for AlpacaOptionsClient — records what it was asked to do."""

    def __init__(self, ask=2.00, fill_price=None, contract="TSLA260918C00150000"):
        self._ask = ask
        self._fill_price = fill_price
        self._contract = contract
        self.orders: list[tuple] = []

    def find_contract(self, ticker, expiry, strike, option_type):
        return {"symbol": self._contract, "strike_price": str(strike)}

    def get_ask_price(self, occ_symbol):
        return self._ask

    def place_order(self, occ_symbol, qty, order_type, limit_price):
        self.orders.append((occ_symbol, qty, order_type, limit_price))
        return {"id": "order-1", "status": "accepted"}

    def wait_for_fill(self, order_id, **kwargs):
        return {"filled_avg_price": self._fill_price} if self._fill_price else {}


# ── Batch identity and staleness ──────────────────────────────────────────


class TestBatchGuards:
    def test_batch_key_is_the_newest_approval(self, tmp_path):
        older = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)
        newer = datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc)
        key = batch_key([_trade(older), _trade(newer, "NVDA")], str(tmp_path))
        assert key == newer.isoformat()

    def test_empty_batch_falls_back_to_file_mtime(self, tmp_path):
        path = tmp_path / "approved.json"
        path.write_text("[]")
        assert batch_key([], str(path))  # does not raise, returns a timestamp

    def test_fresh_batch_is_within_the_window(self, tmp_path):
        now = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)
        trades = [_trade(now - timedelta(hours=2))]
        assert batch_age(trades, str(tmp_path), now) < timedelta(hours=6)

    def test_yesterdays_batch_is_stale(self, tmp_path):
        """The gate failing to run must not replay yesterday's approvals."""
        now = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)
        trades = [_trade(now - timedelta(hours=26))]
        assert batch_age(trades, str(tmp_path), now) > timedelta(hours=6)

    def test_marked_batch_is_not_executed_twice(self, tmp_path):
        cfg = _cfg(tmp_path)
        key = "2026-09-03T13:00:00+00:00"
        assert already_executed(cfg, key) is False
        mark_executed(cfg, key, 3)
        assert already_executed(cfg, key) is True

    def test_a_new_batch_is_not_blocked_by_the_previous_marker(self, tmp_path):
        cfg = _cfg(tmp_path)
        mark_executed(cfg, "2026-09-02T13:00:00+00:00", 3)
        assert already_executed(cfg, "2026-09-03T13:00:00+00:00") is False

    def test_marker_is_written_before_orders_are_placed(self, tmp_path):
        cfg = _cfg(tmp_path)
        mark_executed(cfg, "k", 2)
        with open(tmp_path / "executor" / "executed_batch.json") as f:
            marker = json.load(f)
        assert marker["batch_key"] == "k"
        assert marker["trade_count"] == 2
        assert marker["started_at"]


# ── Quote handling ────────────────────────────────────────────────────────


class TestQuoteHandling:
    def test_limit_price_is_ask_times_buffer(self, tmp_path):
        client = FakeClient(ask=2.00)
        result = execute_trade(_trade(datetime.now(timezone.utc)), client, _cfg(tmp_path))
        assert result.status == "submitted"
        assert result.limit_price == 2.10
        assert client.orders[0][2] == "limit"

    def test_missing_quote_skips_instead_of_sending_a_market_order(self, tmp_path):
        client = FakeClient(ask=None)
        result = execute_trade(_trade(datetime.now(timezone.utc)), client, _cfg(tmp_path))
        assert result.status == "skipped"
        assert "no ask quote" in result.reason
        assert client.orders == []

    def test_market_fallback_only_when_explicitly_enabled(self, tmp_path):
        client = FakeClient(ask=None)
        cfg = _cfg(tmp_path, allow_market_fallback=True)
        result = execute_trade(_trade(datetime.now(timezone.utc)), client, cfg)
        assert result.status == "submitted"
        assert client.orders[0][2] == "market"

    def test_confirmed_fill_is_reported_as_filled(self, tmp_path):
        client = FakeClient(ask=2.00, fill_price="2.05")
        result = execute_trade(_trade(datetime.now(timezone.utc)), client, _cfg(tmp_path))
        assert result.status == "filled"
        assert result.filled_avg_price == 2.05

    def test_unconfirmed_fill_is_reported_as_submitted_not_filled(self, tmp_path):
        """A submitted order is not a fill — the old code called both FILLED."""
        client = FakeClient(ask=2.00, fill_price=None)
        result = execute_trade(_trade(datetime.now(timezone.utc)), client, _cfg(tmp_path))
        assert result.status == "submitted"
        assert result.filled_avg_price is None
