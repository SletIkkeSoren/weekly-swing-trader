"""
Tests for consensus/prompt.py — prompt construction utilities.

These are pure-function tests with no external dependencies.
"""

from datetime import date

import pytest

from consensus.prompt import build_user_message, next_friday

from tests.conftest import make_position, make_snapshot


# ── next_friday ────────────────────────────────────────────────────────────


class TestNextFriday:
    def test_from_monday_returns_same_week_friday(self):
        monday = date(2026, 3, 23)  # Monday
        assert next_friday(monday) == date(2026, 3, 27)

    def test_from_tuesday_returns_same_week_friday(self):
        tuesday = date(2026, 3, 24)
        assert next_friday(tuesday) == date(2026, 3, 27)

    def test_from_thursday_returns_same_week_friday(self):
        thursday = date(2026, 3, 26)
        assert next_friday(thursday) == date(2026, 3, 27)

    def test_from_friday_returns_next_week_friday(self):
        friday = date(2026, 3, 27)
        assert next_friday(friday) == date(2026, 4, 3)

    def test_from_saturday_returns_next_week_friday(self):
        saturday = date(2026, 3, 28)
        assert next_friday(saturday) == date(2026, 4, 3)

    def test_from_sunday_returns_next_week_friday(self):
        sunday = date(2026, 3, 29)
        assert next_friday(sunday) == date(2026, 4, 3)

    def test_result_is_always_a_friday(self):
        from datetime import timedelta
        start = date(2026, 3, 23)
        for offset in range(14):
            d = start + timedelta(days=offset)
            result = next_friday(d)
            assert result.weekday() == 4, f"next_friday({d}) = {result} (weekday {result.weekday()}, expected 4)"


# ── build_user_message ─────────────────────────────────────────────────────


class TestBuildUserMessage:
    def test_contains_ticker(self):
        snapshot = make_snapshot(ticker="TSLA")
        msg = build_user_message(snapshot)
        assert "TSLA" in msg

    def test_contains_close_price(self):
        snapshot = make_snapshot(latest_close=175.50)
        msg = build_user_message(snapshot)
        assert "175.50" in msg

    def test_no_positions_section_when_no_open_positions(self):
        snapshot = make_snapshot(open_positions=[])
        msg = build_user_message(snapshot)
        assert "OPEN POSITIONS" not in msg
        assert "ACTION CONSTRAINT" not in msg

    def test_positions_section_present_when_positions_held(self):
        pos = make_position(pnl_pct=0.50)
        snapshot = make_snapshot(open_positions=[pos])
        msg = build_user_message(snapshot)
        assert "OPEN POSITIONS" in msg

    def test_action_constraint_injected_when_positions_held(self):
        pos = make_position(pnl_pct=0.10)
        snapshot = make_snapshot(open_positions=[pos])
        msg = build_user_message(snapshot)
        assert "CLOSE or HOLD only" in msg

    def test_positive_pnl_formatted_with_plus_sign(self):
        pos = make_position(pnl_pct=0.50)
        snapshot = make_snapshot(open_positions=[pos])
        msg = build_user_message(snapshot)
        assert "+50%" in msg

    def test_negative_pnl_formatted_without_plus_sign(self):
        pos = make_position(pnl_pct=-0.30)
        snapshot = make_snapshot(open_positions=[pos])
        msg = build_user_message(snapshot)
        assert "-30%" in msg
        assert "+-30%" not in msg

    def test_occ_symbol_appears_in_message(self):
        pos = make_position(occ_symbol="TSLA240119C00250000")
        snapshot = make_snapshot(open_positions=[pos])
        msg = build_user_message(snapshot)
        assert "TSLA240119C00250000" in msg

    def test_contains_technical_indicators_section(self):
        snapshot = make_snapshot()
        msg = build_user_message(snapshot)
        assert "TECHNICAL INDICATORS" in msg
        assert "EMA" in msg
        assert "RSI" in msg
        assert "MACD" in msg

    def test_contains_available_expiries_section(self):
        snapshot = make_snapshot()
        msg = build_user_message(snapshot)
        assert "AVAILABLE EXPIRIES" in msg

    def test_contains_recent_price_action(self):
        snapshot = make_snapshot()
        msg = build_user_message(snapshot)
        assert "RECENT PRICE ACTION" in msg

    def test_multiple_positions_all_appear(self):
        positions = [
            make_position(occ_symbol="TSLA240119C00250000", pnl_pct=0.20),
            make_position(occ_symbol="TSLA240119P00200000", pnl_pct=-0.15),
        ]
        snapshot = make_snapshot(open_positions=positions)
        msg = build_user_message(snapshot)
        assert "TSLA240119C00250000" in msg
        assert "TSLA240119P00200000" in msg
