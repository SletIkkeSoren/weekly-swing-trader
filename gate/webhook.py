"""
Human approval webhook.

Sends a TradeProposal as JSON to APPROVAL_WEBHOOK_URL and waits for a
synchronous response: {"approved": true/false, "reason": "..."}

If APPROVAL_WEBHOOK_URL is empty the gate runs in dry-run mode —
proposals are logged but no webhook is called and nothing is approved.
"""

import logging
from datetime import datetime, timezone

import httpx

from gate.config import Config
from gate.models import ApprovedTrade, TradeProposal

log = logging.getLogger(__name__)


async def request_approval(proposal: TradeProposal, cfg: Config) -> ApprovedTrade | None:
    if not cfg.webhook_url:
        log.warning(
            "[%s] DRY RUN — no APPROVAL_WEBHOOK_URL set. Proposal:\n%s",
            proposal.ticker,
            proposal.model_dump_json(indent=2),
        )
        return None

    payload = proposal.model_dump(mode="json")
    log.info("[%s] Sending approval request to webhook", proposal.ticker)

    try:
        async with httpx.AsyncClient(timeout=cfg.webhook_timeout_secs) as client:
            resp = await client.post(cfg.webhook_url, json=payload)
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
