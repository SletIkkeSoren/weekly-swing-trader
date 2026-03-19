"""
Human approval webhook.

Two modes depending on APPROVAL_WEBHOOK_URL:

  Empty URL      → dry-run: logs proposal, nothing approved.

  Discord URL    → posts a formatted embed to Discord and auto-approves.
                   (Discord incoming webhooks are one-way; no response-based
                   approval is possible. Suitable for paper trading.)

  Other URL      → interactive mode: POSTs TradeProposal JSON and waits for
                   {"approved": true/false, "reason": "..."} in response.
                   Use tools/interactive_webhook.py locally or your own endpoint.
"""

import logging
from datetime import datetime, timezone

import httpx

from gate.config import Config
from gate.models import ApprovedTrade, TradeProposal

log = logging.getLogger(__name__)

_DISCORD_HOST = "discord.com/api/webhooks"


def _is_discord(url: str) -> bool:
    return _DISCORD_HOST in url


def _discord_embed(proposal: TradeProposal) -> dict:
    action_emoji = "📈" if proposal.action == "BUY_CALL" else "📉"
    color = 0x2ECC71 if proposal.action == "BUY_CALL" else 0xE74C3C  # green / red

    # One-line summaries: first reasoning point + first invalidating condition
    reasoning = proposal.all_reasoning[0] if proposal.all_reasoning else "—"
    condition = proposal.all_invalidating_conditions[0] if proposal.all_invalidating_conditions else "—"

    return {
        "embeds": [{
            "title": f"{action_emoji} {proposal.ticker} — {proposal.action}",
            "color": color,
            "fields": [
                {"name": "Strike",     "value": f"${proposal.strike:.2f}",             "inline": True},
                {"name": "Expiry",     "value": str(proposal.expiry),                   "inline": True},
                {"name": "Contracts",  "value": str(proposal.suggested_contracts),      "inline": True},
                {"name": "Confidence", "value": f"{proposal.confidence:.0%}",           "inline": True},
                {"name": "Agreement",  "value": f"{proposal.agreement_count}/3 models", "inline": True},
                {"name": "Risk (USD)", "value": f"${proposal.risk_usd:.0f}",            "inline": True},
                {"name": "Thesis",     "value": reasoning[:256]},
                {"name": "Watch if",   "value": condition[:256]},
            ],
            "footer": {"text": "paper trading — auto-approved"},
            "timestamp": proposal.as_of.isoformat(),
        }]
    }


async def request_approval(proposal: TradeProposal, cfg: Config) -> ApprovedTrade | None:
    # ── No URL: dry-run ───────────────────────────────────────────────────────
    if not cfg.webhook_url:
        log.warning(
            "[%s] DRY RUN — no APPROVAL_WEBHOOK_URL set. Proposal:\n%s",
            proposal.ticker,
            proposal.model_dump_json(indent=2),
        )
        return None

    # ── Discord: notify and auto-approve ─────────────────────────────────────
    if _is_discord(cfg.webhook_url):
        log.info("[%s] Posting to Discord and auto-approving (paper trading)", proposal.ticker)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(cfg.webhook_url, json=_discord_embed(proposal))
                resp.raise_for_status()
        except Exception:
            log.exception("[%s] Discord notification failed — still approving", proposal.ticker)
        return ApprovedTrade(
            proposal=proposal,
            approved_at=datetime.now(timezone.utc),
            webhook_reason="discord notification sent, auto-approved for paper trading",
        )

    # ── Interactive: POST and wait for {"approved": bool} response ───────────
    log.info("[%s] Sending approval request to %s", proposal.ticker, cfg.webhook_url)
    try:
        async with httpx.AsyncClient(timeout=cfg.webhook_timeout_secs) as client:
            resp = await client.post(cfg.webhook_url, json=proposal.model_dump(mode="json"))
            resp.raise_for_status()
            body = resp.json()
    except Exception:
        log.exception("[%s] Webhook call failed — rejecting trade", proposal.ticker)
        return None

    if not isinstance(body, dict) or "approved" not in body:
        log.warning("[%s] Webhook returned unexpected body: %r", proposal.ticker, body)
        return None

    if not body["approved"]:
        log.info("[%s] Trade rejected by webhook: %s", proposal.ticker, body.get("reason", ""))
        return None

    log.info("[%s] Trade APPROVED by webhook", proposal.ticker)
    return ApprovedTrade(
        proposal=proposal,
        approved_at=datetime.now(timezone.utc),
        webhook_reason=body.get("reason", ""),
    )
