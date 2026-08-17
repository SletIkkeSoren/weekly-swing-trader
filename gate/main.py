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
from audit.performance import get_ticker_stats

from consensus.models import ConsensusResult
from gate.config import Config
from gate.cooldown import is_on_cooldown, mark_closed
from gate.filters import check_hard_exits, run_all, suggested_contracts
from gate.models import ApprovedTrade, TradeProposal
from gate.webhook import _discord_embed, request_approval

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gate")


async def _discord_notify(payload: dict, cfg: Config, ticker: str, label: str) -> None:
    """Best-effort Discord POST. Never blocks or raises."""
    if not cfg.webhook_url or "discord.com/api/webhooks" not in cfg.webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(cfg.webhook_url, json=payload)
    except Exception:
        log.warning("[%s] Discord notification failed (%s)", ticker, label)


async def _notify_hard_exit(proposal: TradeProposal, cfg: Config) -> None:
    embed = {
        "embeds": [{
            "title": f"🚨 {proposal.ticker} — HARD EXIT",
            "color": 0xE67E22,
            "fields": [
                {"name": "Reason", "value": proposal.all_reasoning[0], "inline": False},
                {"name": "Contracts", "value": str(proposal.suggested_contracts), "inline": True},
            ],
            "footer": {"text": "auto-approved — no human approval required"},
            "timestamp": proposal.as_of.isoformat(),
        }]
    }
    await _discord_notify(embed, cfg, proposal.ticker, "hard exit")


async def _notify_consensus_close(proposal: TradeProposal, cfg: Config) -> None:
    await _discord_notify(_discord_embed(proposal), cfg, proposal.ticker, "consensus close")


async def _notify_hold(result: ConsensusResult, cfg: Config) -> None:
    action_label = result.consensus_action or "NO CONSENSUS"
    confidence_str = f"{result.consensus_confidence:.0%}" if result.consensus_confidence else "n/a"
    pos_lines = "\n".join(
        f"{pos.occ_symbol}: {pos.qty} contract(s) @ ${pos.avg_entry_price:.2f} "
        f"({'+'  if pos.pnl_pct >= 0 else ''}{pos.pnl_pct:.0%}, {pos.days_to_expiry}d to expiry)"
        for pos in result.open_positions
    )
    payload = {
        "embeds": [{
            "title": f"⏸ {result.ticker} — position reviewed, {action_label}",
            "color": 0x3498DB,
            "fields": [
                {"name": "Agreement",   "value": f"{result.agreement_count}/3 models", "inline": True},
                {"name": "Confidence",  "value": confidence_str,                        "inline": True},
                {"name": "Positions",   "value": pos_lines or "—"},
            ],
            "footer": {"text": "no action taken"},
            "timestamp": result.as_of.isoformat(),
        }]
    }
    await _discord_notify(payload, cfg, result.ticker, "hold notification")


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

    ticker_stats = get_ticker_stats()
    log.info("Loaded performance history for %d ticker(s)", len(ticker_stats))

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
            mark_closed(pos.ticker, cfg.output_dir)

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
        filter_result = run_all(result, cfg, open_trade_count=effective_open, ticker_stats=ticker_stats)

        if not filter_result.passed:
            log.info("[%s] REJECTED by filter: %s", result.ticker, filter_result.reason)
            if result.open_positions:
                hold_confidence = result.consensus_confidence or 0.0
                is_weak_hold = (
                    result.consensus_action == "HOLD"
                    and hold_confidence < cfg.min_hold_confidence
                )
                is_no_consensus = result.consensus_action is None
                if is_weak_hold or is_no_consensus:
                    # Weak HOLD or no agreement — models couldn't commit; exit.
                    reason_label = (
                        f"Weak HOLD ({hold_confidence:.0%} confidence)"
                        if is_weak_hold
                        else f"No consensus ({result.agreement_count}/3 models)"
                    )
                    log.warning(
                        "[%s] %s — auto-closing",
                        result.ticker,
                        reason_label,
                    )
                    for pos in result.open_positions:
                        proposal = TradeProposal(
                            ticker=pos.ticker,
                            action="CLOSE",
                            strike=None,
                            expiry=None,
                            confidence=hold_confidence,
                            agreement_count=result.agreement_count,
                            suggested_contracts=pos.qty,
                            risk_usd=0.0,
                            all_reasoning=[f"{reason_label} — auto-close"],
                            all_invalidating_conditions=result.all_invalidating_conditions,
                            as_of=datetime.now(timezone.utc),
                        )
                        trade = ApprovedTrade(
                            proposal=proposal,
                            approved_at=datetime.now(timezone.utc),
                            webhook_reason=f"{reason_label} auto-close",
                        )
                        await _notify_consensus_close(proposal, cfg)
                        approved.append(trade)
                        forced_close_tickers.add(pos.ticker)
                        mark_closed(pos.ticker, cfg.output_dir)
                else:
                    await _notify_hold(result, cfg)
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

        if proposal.action == "CLOSE":
            # Close decisions are self-determined — no human approval needed.
            # The system already required 3/3 model consensus to get here.
            trade = ApprovedTrade(
                proposal=proposal,
                approved_at=datetime.now(timezone.utc),
                webhook_reason="consensus CLOSE auto-approved",
            )
            await _notify_consensus_close(proposal, cfg)
            approved.append(trade)
            mark_closed(proposal.ticker, cfg.output_dir)
        else:
            if is_on_cooldown(proposal.ticker, cfg.output_dir):
                log.info(
                    "[%s] Skipping new %s — ticker on cooldown (position closed earlier today)",
                    proposal.ticker,
                    proposal.action,
                )
                continue
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
