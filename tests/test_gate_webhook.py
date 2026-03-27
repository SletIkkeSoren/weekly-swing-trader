"""
Tests for gate/webhook.py — Discord embed construction.

No HTTP calls are made; we only test the embed payload builder.
"""

from datetime import datetime, timezone

import pytest

from gate.models import TradeProposal
from gate.webhook import _discord_embed


def _proposal(action: str, strike: float | None = 150.0) -> TradeProposal:
    from datetime import date
    return TradeProposal(
        ticker="TSLA",
        action=action,
        strike=strike if action not in ("CLOSE", "HOLD") else None,
        expiry=date(2026, 4, 3) if action not in ("CLOSE", "HOLD") else None,
        confidence=0.75,
        agreement_count=2,
        suggested_contracts=2,
        risk_usd=100.0,
        all_reasoning=["momentum is bullish"],
        all_invalidating_conditions=["RSI drops below 40"],
        as_of=datetime.now(timezone.utc),
    )


# ── Bug 2 regression: CLOSE proposals must not crash ──────────────────────


class TestDiscordEmbed:
    def test_close_proposal_does_not_raise(self):
        """CLOSE has strike=None and expiry=None — must not raise TypeError."""
        embed = _discord_embed(_proposal("CLOSE"))
        assert embed is not None

    def test_close_strike_shown_as_na(self):
        embed = _discord_embed(_proposal("CLOSE"))
        fields = {f["name"]: f["value"] for f in embed["embeds"][0]["fields"]}
        assert fields["Strike"] == "n/a"

    def test_close_expiry_shown_as_na(self):
        embed = _discord_embed(_proposal("CLOSE"))
        fields = {f["name"]: f["value"] for f in embed["embeds"][0]["fields"]}
        assert fields["Expiry"] == "n/a"

    def test_close_uses_neutral_emoji(self):
        embed = _discord_embed(_proposal("CLOSE"))
        title = embed["embeds"][0]["title"]
        assert "🔄" in title
        assert "📈" not in title
        assert "📉" not in title

    def test_buy_call_uses_green_color_and_bullish_emoji(self):
        embed = _discord_embed(_proposal("BUY_CALL"))
        assert embed["embeds"][0]["color"] == 0x2ECC71
        assert "📈" in embed["embeds"][0]["title"]

    def test_buy_put_uses_red_color_and_bearish_emoji(self):
        embed = _discord_embed(_proposal("BUY_PUT"))
        assert embed["embeds"][0]["color"] == 0xE74C3C
        assert "📉" in embed["embeds"][0]["title"]

    def test_buy_call_strike_formatted_as_currency(self):
        embed = _discord_embed(_proposal("BUY_CALL"))
        fields = {f["name"]: f["value"] for f in embed["embeds"][0]["fields"]}
        assert fields["Strike"] == "$150.00"

    def test_reasoning_truncated_to_256_chars(self):
        p = _proposal("BUY_CALL")
        p.all_reasoning[0] = "x" * 300
        embed = _discord_embed(p)
        fields = {f["name"]: f["value"] for f in embed["embeds"][0]["fields"]}
        assert len(fields["Thesis"]) <= 256

    def test_empty_reasoning_shows_dash(self):
        p = _proposal("BUY_CALL")
        p.all_reasoning.clear()
        embed = _discord_embed(p)
        fields = {f["name"]: f["value"] for f in embed["embeds"][0]["fields"]}
        assert fields["Thesis"] == "—"
