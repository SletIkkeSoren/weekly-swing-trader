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
from gate.filters import run_all, suggested_contracts
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

    for result in results:
        filter_result = run_all(result, cfg, open_trade_count=len(approved))

        if not filter_result.passed:
            log.info("[%s] REJECTED by filter: %s", result.ticker, filter_result.reason)
            continue

        contracts = suggested_contracts(result, cfg)
        risk_usd = cfg.account_size * cfg.risk_pct
        proposal = TradeProposal.from_consensus(result, contracts, risk_usd)

        log.info(
            "[%s] PASSED filters — action=%s strike=%.2f expiry=%s contracts=%d risk=$%.0f",
            proposal.ticker,
            proposal.action,
            proposal.strike,
            proposal.expiry,
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
