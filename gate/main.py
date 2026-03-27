"""
Decision gate entrypoint — runs as a k3s CronJob after the consensus engine.

Reads:  $INPUT_DIR/consensus.json   (consensus engine output)
Writes: $OUTPUT_DIR/approved.json   (approved trades for the executor)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

import httpx

from dotenv import load_dotenv

from audit import storage as audit

from consensus.models import ConsensusResult
from gate.config import Config
from gate.filters import check_hard_exits, run_all, suggested_contracts
from gate.models import ApprovedTrade, TradeProposal
from gate.webhook import request_approval

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gate")


async def _notify_hard_exit(proposal: TradeProposal, cfg: Config) -> None:
    """Best-effort Discord notification for hard exits. Never blocks or raises."""
    if not cfg.webhook_url or "discord.com/api/webhooks" not in cfg.webhook_url:
        return
    embed = {
        "embeds": [{
            "title": f"🚨 {proposal.ticker} — HARD EXIT",
            "color": 0xE67E22,  # orange — distinct from buy (green) and put (red)
            "fields": [
                {"name": "Reason", "value": proposal.all_reasoning[0], "inline": False},
                {"name": "Contracts", "value": str(proposal.suggested_contracts), "inline": True},
            ],
            "footer": {"text": "auto-approved — no human approval required"},
            "timestamp": proposal.as_of.isoformat(),
        }]
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(cfg.webhook_url, json=embed)
    except Exception:
        log.warning("[%s] Hard exit Discord notification failed", proposal.ticker)


async def main_async() -> None:
    cfg = Config.from_env()

    consensus_path = os.path.join(cfg.input_dir, "consensus.json")
    if not os.path.exists(consensus_path):
        log.error("consensus.json not found at %s — run consensus engine first", consensus_path)
        sys.exit(1)

    with open(consensus_path) as f:
        raw = json.load(f)

    results = [ConsensusResult.model_validate(r) for r in raw]
    log.info("Loaded %d consensus results", len(results))

    approved: list[ApprovedTrade] = []
    forced_close_tickers: set[str] = set()

    # Baseline: how many option positions are already open on the account
    existing_open_count = sum(len(r.open_positions) for r in results)

    # ── Phase 1: hard exits (override consensus, fire unconditionally) ─────
    for result in results:
        for pos, reason in check_hard_exits(result.open_positions, cfg):
            if pos.ticker in forced_close_tickers:
                continue  # already queued a close for this ticker
            log.warning("[%s] HARD EXIT: %s", pos.ticker, reason)
            proposal = TradeProposal(
                ticker=pos.ticker,
                action="CLOSE",
                strike=None,
                expiry=None,
                confidence=1.0,
                agreement_count=3,
                suggested_contracts=pos.qty,
                risk_usd=0.0,
                all_reasoning=[f"FORCED CLOSE: {reason}"],
                all_invalidating_conditions=[],
                as_of=datetime.now(timezone.utc),
            )
            # Hard exits bypass the approval webhook — they fire unconditionally.
            # The webhook is never consulted for approval; a human cannot veto a
            # stop-loss. We still send a Discord notification if configured.
            trade = ApprovedTrade(
                proposal=proposal,
                approved_at=datetime.now(timezone.utc),
                webhook_reason=f"hard exit auto-approved: {reason}",
            )
            await _notify_hard_exit(proposal, cfg)
            approved.append(trade)
            forced_close_tickers.add(pos.ticker)

    # ── Phase 2: consensus-based filter chain ──────────────────────────────
    for result in results:
        if result.ticker in forced_close_tickers:
            log.info("[%s] Skipping consensus — already queued a forced close", result.ticker)
            continue

        # Effective open positions = existing + new buys approved − hard-exit closes.
        # Count actual positions closed (a ticker may have multiple contracts), not
        # just unique tickers, to avoid inflating the open count.
        positions_closed = sum(
            len(r.open_positions)
            for r in results
            if r.ticker in forced_close_tickers
        )
        new_buys = sum(1 for t in approved if t.proposal.action != "CLOSE")
        effective_open = existing_open_count - positions_closed + new_buys
        filter_result = run_all(result, cfg, open_trade_count=effective_open)

        if not filter_result.passed:
            log.info("[%s] REJECTED by filter: %s", result.ticker, filter_result.reason)
            continue

        contracts = suggested_contracts(cfg)
        risk_usd = cfg.account_size * cfg.risk_pct
        proposal = TradeProposal.from_consensus(result, contracts, risk_usd)

        strike_str = f"{proposal.strike:.2f}" if proposal.strike is not None else "n/a"
        log.info(
            "[%s] PASSED filters — action=%s strike=%s expiry=%s contracts=%d risk=$%.0f",
            proposal.ticker,
            proposal.action,
            strike_str,
            proposal.expiry or "n/a",
            proposal.suggested_contracts,
            proposal.risk_usd,
        )

        trade = await request_approval(proposal, cfg)
        if trade:
            approved.append(trade)

    log.info("%d/%d trades approved", len(approved), len(results))
    audit.record_approved(approved)

    os.makedirs(cfg.output_dir, exist_ok=True)
    out_path = os.path.join(cfg.output_dir, "approved.json")
    payload = [t.model_dump(mode="json") for t in approved]
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    log.info("Wrote approved trades to %s", out_path)

    print(json.dumps(payload, indent=2, default=str))


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
