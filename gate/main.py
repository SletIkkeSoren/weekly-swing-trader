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

from dotenv import load_dotenv

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
            trade = await request_approval(proposal, cfg)
            if trade:
                approved.append(trade)
            forced_close_tickers.add(pos.ticker)

    # ── Phase 2: consensus-based filter chain ──────────────────────────────
    for result in results:
        if result.ticker in forced_close_tickers:
            log.info("[%s] Skipping consensus — already queued a forced close", result.ticker)
            continue

        # Effective open positions = existing + new buys approved − hard-exit closes
        new_buys = sum(1 for t in approved if t.proposal.action != "CLOSE")
        effective_open = existing_open_count - len(forced_close_tickers) + new_buys
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
