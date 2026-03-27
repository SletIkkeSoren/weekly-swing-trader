"""
Consensus engine entrypoint — runs as a k3s CronJob after the fetcher.

Reads:  $INPUT_DIR/snapshots.json   (fetcher output)
Writes: $OUTPUT_DIR/consensus.json  (passed results only, for the decision gate)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from audit import storage as audit
from consensus.config import Config
from consensus.engine import evaluate_all
from fetcher.models import MarketSnapshot

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("consensus")


async def main_async() -> None:
    cfg = Config.from_env()

    snapshots_path = os.path.join(cfg.input_dir, "snapshots.json")
    if not os.path.exists(snapshots_path):
        log.error("snapshots.json not found at %s — run fetcher first", snapshots_path)
        sys.exit(1)

    with open(snapshots_path) as f:
        raw = json.load(f)

    snapshots = [MarketSnapshot.model_validate(s) for s in raw]
    log.info("Loaded %d snapshots — starting consensus evaluation", len(snapshots))

    results = await evaluate_all(snapshots, cfg)

    passed = [r for r in results if r.passed]
    log.info(
        "Evaluation complete — %d/%d tickers passed consensus",
        len(passed), len(results),
    )

    os.makedirs(cfg.output_dir, exist_ok=True)
    out_path = os.path.join(cfg.output_dir, "consensus.json")
    payload = [r.model_dump(mode="json") for r in passed]
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    log.info("Wrote %d passed results to %s", len(passed), out_path)
    audit.record_consensus(results)  # audit all results, not just passed

    print(json.dumps(payload, indent=2, default=str))


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
